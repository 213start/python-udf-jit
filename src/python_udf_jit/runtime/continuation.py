from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from types import CodeType, MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar

from python_udf_jit.compiler.identity import (
    SourceIdentity,
    code_identity_from_code,
    verify_source_identity,
)
from python_udf_jit.runtime.layout import (
    BOOL_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
    normalize_scalar_value,
)


class ContinuationError(RuntimeError):
    """A region cannot safely transfer to its interpreter continuation."""


CONTINUATION_ABI_VERSION = 1


class SideExitOrigin(StrEnum):
    REGION_SIDE_EXIT = "region_side_exit"
    GRAPH_BREAK = "graph_break"
    GUARD_MISS = "guard_miss"
    CINDERX_DEOPT = "cinderx_deopt"
    INTERNAL_FAILURE = "internal_failure"


class RecoveryScope(StrEnum):
    REGION = "region"
    WHOLE_FUNCTION = "whole_function"


class LiveValueKind(StrEnum):
    PYTHON_OBJECT = "python_object"
    BOOL = BOOL_SCALAR_TYPE
    INT32 = INT32_SCALAR_TYPE
    INT64 = INT64_SCALAR_TYPE
    FLOAT32 = FLOAT32_SCALAR_TYPE
    FLOAT64 = FLOAT64_SCALAR_TYPE


class MaterializationState(StrEnum):
    MATERIALIZED = "materialized"
    FAILED = "failed"


class CommitPhase(StrEnum):
    PRE_COMMIT = "pre_commit"
    COMMITTED = "committed"
    SUFFIX_CLAIMED = "suffix_claimed"
    WHOLE_FUNCTION_CLAIMED = "whole_function_claimed"


_RESUME_ID = re.compile(
    r"^v([1-9][0-9]*):"
    r"(?:[a-z][a-z0-9_.-]{0,63}|[0-9a-f]{64})$"
)


def _unique_names(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if (
        not isinstance(values, tuple)
        or len(set(values)) != len(values)
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise ContinuationError(f"{field}_invalid")
    return values


@dataclass(frozen=True)
class ResumeSourceMap:
    """Path-free source position for one versioned continuation entry."""

    schema_version: int
    bytecode_offset: int
    line: int
    column: int | None
    end_line: int
    end_column: int | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ContinuationError("source_map_schema_version_invalid")
        for field in ("bytecode_offset", "line", "end_line"):
            value = getattr(self, field)
            minimum = 0 if field == "bytecode_offset" else 1
            if type(value) is not int or value < minimum:
                raise ContinuationError(f"source_map_{field}_invalid")
        for field in ("column", "end_column"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ContinuationError(f"source_map_{field}_invalid")
        if self.end_line < self.line:
            raise ContinuationError("source_map_range_invalid")


@dataclass(frozen=True)
class LiveValueSpec:
    """Exact representation required at one continuation entry."""

    name: str
    kind: LiveValueKind
    nullable: bool = False
    branch_join: bool = False
    borrowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ContinuationError("live_value_name_invalid")
        if not isinstance(self.kind, LiveValueKind):
            raise ContinuationError(f"live_value_kind_invalid:{self.name}")
        for field in ("nullable", "branch_join", "borrowed"):
            if type(getattr(self, field)) is not bool:
                raise ContinuationError(
                    f"live_value_{field}_invalid:{self.name}"
                )
        if self.borrowed and self.kind is not LiveValueKind.PYTHON_OBJECT:
            raise ContinuationError(
                f"borrowed_live_value_must_be_object:{self.name}"
            )


@dataclass(frozen=True)
class MaterializedLiveValue:
    """A CinderX/Python value plus an explicit materialization outcome."""

    kind: LiveValueKind
    state: MaterializationState
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LiveValueKind):
            raise ContinuationError("materialized_live_value_kind_invalid")
        if not isinstance(self.state, MaterializationState):
            raise ContinuationError("materialized_live_value_state_invalid")
        if (
            self.state is MaterializationState.FAILED
            and self.value is not None
        ):
            raise ContinuationError("failed_materialization_has_value")

    @classmethod
    def materialized(
        cls,
        kind: LiveValueKind,
        value: Any,
    ) -> MaterializedLiveValue:
        return cls(kind, MaterializationState.MATERIALIZED, value)

    @classmethod
    def failed(cls, kind: LiveValueKind) -> MaterializedLiveValue:
        return cls(kind, MaterializationState.FAILED)


def _verify_materialized_value(
    spec: LiveValueSpec,
    materialized: MaterializedLiveValue,
) -> None:
    name = spec.name
    if materialized.state is not MaterializationState.MATERIALIZED:
        raise ContinuationError(f"live_value_materialization_failed:{name}")
    if materialized.kind is not spec.kind:
        raise ContinuationError(f"live_value_kind_mismatch:{name}")
    value = materialized.value
    if value is None:
        if not spec.nullable:
            raise ContinuationError(f"unexpected_null:{name}")
        return
    if spec.kind is LiveValueKind.PYTHON_OBJECT:
        return
    try:
        normalized = normalize_scalar_value(
            value,
            spec.kind.value,
            nullable=spec.nullable,
        )
    except (TypeError, ValueError, OverflowError):
        valid = False
    else:
        valid = (
            spec.kind is not LiveValueKind.FLOAT32
            or not math.isfinite(value)
            or normalized == value
        )
    if not valid:
        raise ContinuationError(f"live_value_type_mismatch:{name}")


@dataclass(frozen=True)
class ContinuationContract:
    """Pre-execution proof for a cross-code-object region continuation."""

    abi_version: int
    resume_id: str
    source_identity: SourceIdentity
    source_code: CodeType
    resume_code: CodeType
    source_map: ResumeSourceMap
    live_values: tuple[LiveValueSpec, ...]
    alias_groups: tuple[tuple[str, ...], ...] = ()
    preserves_active_exception: bool = False
    proof_complete: bool = False

    def __post_init__(self) -> None:
        if self.abi_version != CONTINUATION_ABI_VERSION:
            raise ContinuationError("continuation_abi_mismatch")
        match = (
            _RESUME_ID.fullmatch(self.resume_id)
            if isinstance(self.resume_id, str)
            else None
        )
        if match is None:
            raise ContinuationError("resume_id_invalid")
        if int(match.group(1)) != self.abi_version:
            raise ContinuationError("resume_id_version_mismatch")
        if not isinstance(self.source_identity, SourceIdentity):
            raise ContinuationError("source_identity_invalid")
        try:
            verify_source_identity(self.source_identity)
        except ValueError as error:
            raise ContinuationError("source_identity_invalid") from error
        if not isinstance(self.source_code, CodeType):
            raise ContinuationError("source_code_invalid")
        if not isinstance(self.resume_code, CodeType):
            raise ContinuationError("resume_code_invalid")
        if not isinstance(self.source_map, ResumeSourceMap):
            raise ContinuationError("source_map_invalid")
        if self.source_map.schema_version != self.abi_version:
            raise ContinuationError("source_map_version_mismatch")
        if self.source_identity.first_line != self.source_code.co_firstlineno:
            raise ContinuationError("source_identity_line_mismatch")
        if (
            self.source_identity.code_sha256
            != code_identity_from_code(self.source_code).sha256
        ):
            raise ContinuationError("source_identity_code_mismatch")
        if (
            not isinstance(self.live_values, tuple)
            or any(
                not isinstance(spec, LiveValueSpec)
                for spec in self.live_values
            )
        ):
            raise ContinuationError("live_values_invalid")
        live_names = tuple(spec.name for spec in self.live_values)
        _unique_names(live_names, "live_names")
        if not isinstance(self.alias_groups, tuple):
            raise ContinuationError("alias_groups_invalid")
        seen_groups: set[tuple[str, ...]] = set()
        live_by_name = {spec.name: spec for spec in self.live_values}
        for group in self.alias_groups:
            names = _unique_names(group, "alias_group")
            if len(names) < 2 or names in seen_groups:
                raise ContinuationError("alias_group_invalid")
            if not set(names) <= set(live_names):
                raise ContinuationError("alias_group_not_live")
            if any(
                live_by_name[name].kind is not LiveValueKind.PYTHON_OBJECT
                for name in names
            ):
                raise ContinuationError("alias_group_must_be_object")
            seen_groups.add(names)
        for field in ("preserves_active_exception", "proof_complete"):
            if type(getattr(self, field)) is not bool:
                raise ContinuationError(f"{field}_invalid")

    @property
    def live_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.live_values)

    @property
    def borrowed_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.live_values if spec.borrowed)

    @property
    def is_proven(self) -> bool:
        """Whether region entry is safe; otherwise select whole-function CPython."""

        return self.proof_complete


class CommitBoundary:
    """One transaction boundary shared by a Region and its suffix."""

    __slots__ = ("_lock", "_phase")

    def __init__(self) -> None:
        self._phase = CommitPhase.PRE_COMMIT
        self._lock = threading.Lock()

    @property
    def phase(self) -> CommitPhase:
        with self._lock:
            return self._phase

    @property
    def committed(self) -> bool:
        with self._lock:
            return (
                self._phase is CommitPhase.COMMITTED
                or self._phase is CommitPhase.SUFFIX_CLAIMED
            )

    def require_pre_commit(self) -> None:
        with self._lock:
            if self._phase is not CommitPhase.PRE_COMMIT:
                raise ContinuationError("fresh_commit_boundary_required")

    def commit(self) -> None:
        with self._lock:
            if self._phase is not CommitPhase.PRE_COMMIT:
                raise ContinuationError("commit_already_recorded")
            self._phase = CommitPhase.COMMITTED

    def claim_suffix(self) -> None:
        with self._lock:
            if self._phase is CommitPhase.PRE_COMMIT:
                raise ContinuationError("commit_required_before_suffix")
            if self._phase is not CommitPhase.COMMITTED:
                raise ContinuationError("suffix_already_claimed")
            self._phase = CommitPhase.SUFFIX_CLAIMED

    def claim_whole_function(self) -> None:
        with self._lock:
            if self._phase is CommitPhase.WHOLE_FUNCTION_CLAIMED:
                raise ContinuationError("whole_function_already_claimed")
            if self._phase is not CommitPhase.PRE_COMMIT:
                raise ContinuationError(
                    "whole_function_replay_after_commit"
                )
            self._phase = CommitPhase.WHOLE_FUNCTION_CLAIMED

    def __reduce__(self) -> object:
        raise TypeError("commit boundaries are process-local")


@dataclass(frozen=True)
class ContinuationState:
    """Shallow live-value snapshot retaining identity, aliases, and keepalives."""

    contract: ContinuationContract
    materialized_values: Mapping[str, MaterializedLiveValue]
    values: Mapping[str, Any]
    active_exception: BaseException | None
    _keepalives: Mapping[str, Any]

    @classmethod
    def capture(
        cls,
        contract: ContinuationContract,
        live_values: Mapping[str, MaterializedLiveValue],
        *,
        active_exception: BaseException | None = None,
        keepalives: Mapping[str, Any] | None = None,
    ) -> ContinuationState:
        if not isinstance(contract, ContinuationContract):
            raise ContinuationError("contract_invalid")
        if not isinstance(live_values, Mapping):
            raise ContinuationError("live_values_invalid")
        if set(live_values) != set(contract.live_names):
            raise ContinuationError("live_value_names_mismatch")
        materialized = dict(live_values)
        values: dict[str, Any] = {}
        for spec in contract.live_values:
            value = materialized[spec.name]
            if not isinstance(value, MaterializedLiveValue):
                raise ContinuationError(
                    f"materialized_live_value_required:{spec.name}"
                )
            _verify_materialized_value(spec, value)
            values[spec.name] = value.value
        for group in contract.alias_groups:
            first = values[group[0]]
            if any(values[name] is not first for name in group[1:]):
                raise ContinuationError("live_value_alias_mismatch")
        if contract.preserves_active_exception:
            if not isinstance(active_exception, BaseException):
                raise ContinuationError("active_exception_missing")
        elif active_exception is not None:
            raise ContinuationError("active_exception_unproven")
        retained = {} if keepalives is None else dict(keepalives)
        if set(retained) != set(contract.borrowed_names):
            raise ContinuationError("borrow_keepalive_mismatch")
        for name, value in retained.items():
            if value is None or value is not values[name]:
                raise ContinuationError(f"borrow_keepalive_mismatch:{name}")
        return cls(
            contract=contract,
            materialized_values=MappingProxyType(materialized),
            values=MappingProxyType(values),
            active_exception=active_exception,
            _keepalives=MappingProxyType(retained),
        )

    def __reduce__(self) -> object:
        raise TypeError("continuation state is process-local")


@dataclass(frozen=True)
class SideExit:
    abi_version: int
    reason: str
    resume_id: str
    source_identity: SourceIdentity
    source_map: ResumeSourceMap
    state: ContinuationState
    boundary: CommitBoundary
    origin: SideExitOrigin
    recovery_scope: RecoveryScope = RecoveryScope.REGION

    def __post_init__(self) -> None:
        if self.abi_version != CONTINUATION_ABI_VERSION:
            raise ContinuationError("side_exit_abi_mismatch")
        if not isinstance(self.reason, str) or not self.reason:
            raise ContinuationError("side_exit_reason_invalid")
        if not isinstance(self.resume_id, str) or not self.resume_id:
            raise ContinuationError("side_exit_resume_id_invalid")
        if not isinstance(self.source_map, ResumeSourceMap):
            raise ContinuationError("side_exit_source_map_invalid")
        if not isinstance(self.state, ContinuationState):
            raise ContinuationError("side_exit_state_invalid")
        if self.abi_version != self.state.contract.abi_version:
            raise ContinuationError("side_exit_contract_abi_mismatch")
        if self.source_identity != self.state.contract.source_identity:
            raise ContinuationError("side_exit_source_identity_mismatch")
        if self.source_map != self.state.contract.source_map:
            raise ContinuationError("side_exit_source_map_mismatch")
        if not isinstance(self.boundary, CommitBoundary):
            raise ContinuationError("side_exit_boundary_invalid")
        if not isinstance(self.origin, SideExitOrigin):
            raise ContinuationError("side_exit_origin_invalid")
        if not isinstance(self.recovery_scope, RecoveryScope):
            raise ContinuationError("side_exit_scope_invalid")
        if self.recovery_scope is not RecoveryScope.REGION:
            raise ContinuationError("side_exit_scope_must_be_region")
        if not self.boundary.committed:
            raise ContinuationError("side_exit_requires_commit")


_CINDERX_CONTINUATION_PAYLOAD_FIELD_COUNT = 10


def side_exit_from_cinderx_payload(
    payload: object,
    *,
    contract: ContinuationContract,
    boundary: CommitBoundary,
) -> SideExit:
    """Decode the immutable payload emitted by CinderX after Region deopt."""

    if (
        type(payload) is not tuple
        or len(payload) != _CINDERX_CONTINUATION_PAYLOAD_FIELD_COUNT
        or type(payload[0]) is not int
        or type(payload[1]) is not str
        or type(payload[2]) is not str
        or type(payload[3]) is not str
        or type(payload[4]) is not str
        or type(payload[5]) is not int
        or type(payload[6]) is not tuple
        or type(payload[7]) is not tuple
        or type(payload[9]) is not bool
    ):
        raise ContinuationError("cinderx_continuation_payload_invalid")
    (
        abi_version,
        reason,
        resume_id,
        namespace_sha256,
        code_sha256,
        first_line,
        source_position,
        live_payloads,
        active_exception,
        committed,
    ) = payload
    if abi_version != contract.abi_version:
        raise ContinuationError("cinderx_continuation_abi_mismatch")
    try:
        source_identity = SourceIdentity.from_document(
            {
                "format_version": abi_version,
                "namespace_sha256": namespace_sha256,
                "code_sha256": code_sha256,
                "first_line": first_line,
            }
        )
    except ValueError as error:
        raise ContinuationError(
            "cinderx_continuation_source_identity_invalid"
        ) from error
    if source_identity != contract.source_identity:
        raise ContinuationError(
            "cinderx_continuation_source_identity_mismatch"
        )
    if (
        len(source_position) != 6
        or any(
            value is not None and type(value) is not int
            for value in source_position
        )
    ):
        raise ContinuationError("cinderx_continuation_source_map_invalid")
    source_map = ResumeSourceMap(*source_position)
    if source_map != contract.source_map:
        raise ContinuationError("cinderx_continuation_source_map_mismatch")
    if committed is not True or not boundary.committed:
        raise ContinuationError("cinderx_continuation_commit_mismatch")
    if len(live_payloads) != len(contract.live_values):
        raise ContinuationError(
            "cinderx_continuation_live_value_shape_mismatch"
        )

    live_values: dict[str, MaterializedLiveValue] = {}
    keepalives: dict[str, Any] = {}
    for spec, entry in zip(
        contract.live_values,
        live_payloads,
        strict=True,
    ):
        if (
            type(entry) is not tuple
            or len(entry) != 4
            or type(entry[0]) is not str
            or type(entry[1]) is not bool
            or type(entry[2]) is not bool
        ):
            raise ContinuationError(
                f"cinderx_continuation_live_value_invalid:{spec.name}"
            )
        kind_name, nullable, materialized, value = entry
        try:
            kind = LiveValueKind(kind_name)
        except ValueError as error:
            raise ContinuationError(
                f"cinderx_continuation_live_value_kind_invalid:{spec.name}"
            ) from error
        if kind is not spec.kind or nullable is not spec.nullable:
            raise ContinuationError(
                f"cinderx_continuation_live_value_spec_mismatch:{spec.name}"
            )
        live_values[spec.name] = MaterializedLiveValue(
            kind,
            (
                MaterializationState.MATERIALIZED
                if materialized
                else MaterializationState.FAILED
            ),
            value,
        )
        if spec.borrowed:
            keepalives[spec.name] = value
    state = ContinuationState.capture(
        contract,
        live_values,
        active_exception=active_exception,
        keepalives=keepalives,
    )
    return SideExit(
        abi_version=abi_version,
        reason=reason,
        resume_id=resume_id,
        source_identity=source_identity,
        source_map=source_map,
        state=state,
        boundary=boundary,
        origin=SideExitOrigin.CINDERX_DEOPT,
    )


def side_exit_from_cinderx_result(
    result: object,
    *,
    contract: ContinuationContract,
    boundary: CommitBoundary,
) -> SideExit | None:
    """Decode a CinderX continuation result without leaking tuple shape."""

    if (
        type(result) is not tuple
        or len(result) != _CINDERX_CONTINUATION_PAYLOAD_FIELD_COUNT
    ):
        return None
    return side_exit_from_cinderx_payload(
        result,
        contract=contract,
        boundary=boundary,
    )


R = TypeVar("R")


class InterpreterContinuation(Generic[R]):
    """Explicit SideExit -> CPython suffix transfer within one scalar Provider."""

    __slots__ = ("_contract", "_resume")

    def __init__(
        self,
        contract: ContinuationContract,
        resume: Callable[[ContinuationState], R],
    ) -> None:
        if not isinstance(contract, ContinuationContract):
            raise ContinuationError("contract_invalid")
        if not callable(resume):
            raise ContinuationError("resume_callable_invalid")
        if resume.__code__ is not contract.resume_code:
            raise ContinuationError("resume_code_identity_mismatch")
        self._contract = contract
        self._resume = resume

    @property
    def contract(self) -> ContinuationContract:
        return self._contract

    def resume(self, side_exit: SideExit) -> R:
        if not isinstance(side_exit, SideExit):
            raise ContinuationError("side_exit_required")
        if not self._contract.is_proven:
            raise ContinuationError("continuation_proof_incomplete")
        if side_exit.state.contract != self._contract:
            raise ContinuationError("continuation_contract_mismatch")
        if side_exit.resume_id != self._contract.resume_id:
            raise ContinuationError("resume_id_mismatch")
        side_exit.boundary.claim_suffix()
        return self._resume(side_exit.state)

    def __reduce__(self) -> object:
        raise TypeError("interpreter continuations are process-local")


class WholeFunctionInterpreter(Generic[R]):
    """Pre-entry fallback used when the Region continuation proof is incomplete."""

    __slots__ = ("_original",)

    def __init__(self, original: Callable[..., R]) -> None:
        if not callable(original):
            raise ContinuationError("original_callable_invalid")
        self._original = original

    def execute(
        self,
        boundary: CommitBoundary,
        *args: Any,
        **kwargs: Any,
    ) -> R:
        if not isinstance(boundary, CommitBoundary):
            raise ContinuationError("commit_boundary_required")
        boundary.claim_whole_function()
        return self._original(*args, **kwargs)

    def __reduce__(self) -> object:
        raise TypeError("whole-function interpreters are process-local")


def select_interpreter_path(
    contract: ContinuationContract,
    *,
    resume: Callable[[ContinuationState], R],
    original: Callable[..., R],
) -> InterpreterContinuation[R] | WholeFunctionInterpreter[R]:
    """Select before Region execution so an unproven path cannot replay effects."""

    if contract.is_proven:
        return InterpreterContinuation(contract, resume)
    return WholeFunctionInterpreter(original)
