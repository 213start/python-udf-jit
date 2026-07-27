from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from types import CodeType, MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar


class ContinuationError(RuntimeError):
    """A region cannot safely transfer to its interpreter continuation."""


class SideExitOrigin(StrEnum):
    REGION_SIDE_EXIT = "region_side_exit"
    GRAPH_BREAK = "graph_break"
    GUARD_MISS = "guard_miss"
    CINDERX_DEOPT = "cinderx_deopt"
    INTERNAL_FAILURE = "internal_failure"


class RecoveryScope(StrEnum):
    REGION = "region"
    WHOLE_FUNCTION = "whole_function"


_RESUME_ID = re.compile(r"^v([1-9][0-9]*):[a-z][a-z0-9_.-]{0,63}$")


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
class ContinuationContract:
    """Pre-execution proof for a cross-code-object region continuation."""

    contract_version: int
    resume_id: str
    source_code: CodeType
    resume_code: CodeType
    source_map: ResumeSourceMap
    live_names: tuple[str, ...]
    nullable_names: tuple[str, ...] = ()
    branch_join_names: tuple[str, ...] = ()
    borrowed_names: tuple[str, ...] = ()
    preserves_aliases: bool = False
    preserves_active_exception: bool = False
    commit_required: bool = True
    proof_complete: bool = False

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version <= 0:
            raise ContinuationError("contract_version_invalid")
        match = (
            _RESUME_ID.fullmatch(self.resume_id)
            if isinstance(self.resume_id, str)
            else None
        )
        if match is None:
            raise ContinuationError("resume_id_invalid")
        if int(match.group(1)) != self.contract_version:
            raise ContinuationError("resume_id_version_mismatch")
        if not isinstance(self.source_code, CodeType):
            raise ContinuationError("source_code_invalid")
        if not isinstance(self.resume_code, CodeType):
            raise ContinuationError("resume_code_invalid")
        if not isinstance(self.source_map, ResumeSourceMap):
            raise ContinuationError("source_map_invalid")
        if self.source_map.schema_version != self.contract_version:
            raise ContinuationError("source_map_version_mismatch")
        live = _unique_names(self.live_names, "live_names")
        nullable = _unique_names(self.nullable_names, "nullable_names")
        joined = _unique_names(self.branch_join_names, "branch_join_names")
        borrowed = _unique_names(self.borrowed_names, "borrowed_names")
        if not set(nullable) <= set(live):
            raise ContinuationError("nullable_not_live")
        if not set(joined) <= set(live):
            raise ContinuationError("branch_join_not_live")
        if not set(borrowed) <= set(live):
            raise ContinuationError("borrowed_not_live")
        for field in (
            "preserves_aliases",
            "preserves_active_exception",
            "commit_required",
            "proof_complete",
        ):
            if type(getattr(self, field)) is not bool:
                raise ContinuationError(f"{field}_invalid")

    @property
    def is_proven(self) -> bool:
        """Whether region entry is safe; otherwise select whole-function CPython."""

        return (
            self.proof_complete
            and self.preserves_aliases
            and self.commit_required
            and (
                not self.borrowed_names
                or set(self.borrowed_names) <= set(self.live_names)
            )
        )


class CommitBoundary:
    """One transaction boundary shared by a Region and its suffix."""

    __slots__ = ("_committed", "_lock", "_suffix_claimed")

    def __init__(self) -> None:
        self._committed = False
        self._suffix_claimed = False
        self._lock = threading.RLock()

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._committed

    def commit(self) -> None:
        with self._lock:
            if self._committed:
                raise ContinuationError("commit_already_recorded")
            self._committed = True

    def claim_suffix(self, *, require_commit: bool) -> None:
        with self._lock:
            if require_commit and not self._committed:
                raise ContinuationError("commit_required_before_suffix")
            if self._suffix_claimed:
                raise ContinuationError("suffix_already_claimed")
            self._suffix_claimed = True

    def __reduce__(self) -> object:
        raise TypeError("commit boundaries are process-local")


@dataclass(frozen=True)
class ContinuationState:
    """Shallow live-value snapshot retaining identity, aliases, and keepalives."""

    contract: ContinuationContract
    values: Mapping[str, Any]
    active_exception: BaseException | None
    _keepalives: Mapping[str, Any]

    @classmethod
    def capture(
        cls,
        contract: ContinuationContract,
        live_values: Mapping[str, Any],
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
        values = dict(live_values)
        for name, value in values.items():
            if value is None and name not in contract.nullable_names:
                raise ContinuationError(f"unexpected_null:{name}")
        if contract.preserves_active_exception:
            if not isinstance(active_exception, BaseException):
                raise ContinuationError("active_exception_missing")
        elif active_exception is not None:
            raise ContinuationError("active_exception_unproven")
        retained = {} if keepalives is None else dict(keepalives)
        if set(retained) != set(contract.borrowed_names):
            raise ContinuationError("borrow_keepalive_mismatch")
        if any(value is None for value in retained.values()):
            raise ContinuationError("borrow_keepalive_missing")
        return cls(
            contract=contract,
            values=MappingProxyType(values),
            active_exception=active_exception,
            _keepalives=MappingProxyType(retained),
        )

    def __reduce__(self) -> object:
        raise TypeError("continuation state is process-local")


@dataclass(frozen=True)
class SideExit:
    reason: str
    resume_id: str
    source_map: ResumeSourceMap
    state: ContinuationState
    boundary: CommitBoundary
    origin: SideExitOrigin
    recovery_scope: RecoveryScope = RecoveryScope.REGION

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise ContinuationError("side_exit_reason_invalid")
        if not isinstance(self.resume_id, str) or not self.resume_id:
            raise ContinuationError("side_exit_resume_id_invalid")
        if not isinstance(self.source_map, ResumeSourceMap):
            raise ContinuationError("side_exit_source_map_invalid")
        if not isinstance(self.state, ContinuationState):
            raise ContinuationError("side_exit_state_invalid")
        if self.source_map != self.state.contract.source_map:
            raise ContinuationError("side_exit_source_map_mismatch")
        if not isinstance(self.boundary, CommitBoundary):
            raise ContinuationError("side_exit_boundary_invalid")
        if not isinstance(self.origin, SideExitOrigin):
            raise ContinuationError("side_exit_origin_invalid")
        if not isinstance(self.recovery_scope, RecoveryScope):
            raise ContinuationError("side_exit_scope_invalid")
        if (
            self.origin is SideExitOrigin.CINDERX_DEOPT
            and self.recovery_scope is not RecoveryScope.REGION
        ):
            raise ContinuationError("deopt_scope_must_be_region")


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
        if (
            side_exit.resume_id != self._contract.resume_id
            or side_exit.state.contract.resume_id != self._contract.resume_id
        ):
            raise ContinuationError("resume_id_mismatch")
        if side_exit.recovery_scope is not RecoveryScope.REGION:
            raise ContinuationError("region_recovery_required")
        side_exit.boundary.claim_suffix(
            require_commit=self._contract.commit_required,
        )
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

    def execute(self, *args: Any, **kwargs: Any) -> R:
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
