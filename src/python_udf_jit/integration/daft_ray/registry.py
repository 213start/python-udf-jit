from __future__ import annotations

import hashlib
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from python_udf_jit.compiler.capture import CaptureIR, CaptureRequest, try_capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact


@dataclass
class CandidateRecord:
    candidate_id: str
    wrapper: FallbackOnlyWrapper
    expression_id: int | None = None
    finalized: bool = False
    capture_ir: CaptureIR | None = None


def _candidate_id(func: Any, original_callable: Callable[..., Any]) -> str:
    code = getattr(original_callable, "__code__", None)
    code_bytes = getattr(code, "co_code", b"")
    identity = "\0".join(
        (
            str(getattr(func, "func_id", "")),
            str(getattr(original_callable, "__module__", "")),
            str(getattr(original_callable, "__qualname__", "")),
        )
    ).encode("utf-8") + bytes(code_bytes)
    return hashlib.sha256(identity).hexdigest()


class CandidateRegistry:
    """Bounded, process-local registry; it is never serialized to a Worker."""

    def __init__(self, manifest_sha256: str, max_candidates: int = 1024):
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self._manifest_sha256 = manifest_sha256
        self._max_candidates = max_candidates
        self._records: OrderedDict[int, CandidateRecord] = OrderedDict()
        self._func_refs: dict[int, weakref.ReferenceType[Any] | None] = {}
        self._expression_records: dict[int, CandidateRecord] = {}
        self._expression_refs: dict[int, weakref.ReferenceType[Any] | None] = {}
        self._lock = threading.RLock()
        self.registration_count = 0
        self.finalization_count = 0

    def _drop_func(self, func_id: int) -> None:
        with self._lock:
            self._records.pop(func_id, None)
            self._func_refs.pop(func_id, None)

    def _drop_expression(self, expression_id: int) -> None:
        with self._lock:
            self._expression_records.pop(expression_id, None)
            self._expression_refs.pop(expression_id, None)

    @staticmethod
    def _weakref_or_none(value: Any, callback) -> weakref.ReferenceType[Any] | None:
        try:
            return weakref.ref(value, callback)
        except TypeError:
            return None

    def register(
        self, func: Any, original_callable: Callable[..., Any]
    ) -> CandidateRecord:
        func_id = id(func)
        with self._lock:
            existing = self._records.get(func_id)
            if existing is not None:
                self._records.move_to_end(func_id)
                return existing

            candidate_id = _candidate_id(func, original_callable)
            wrapper = FallbackOnlyWrapper(
                candidate_id=candidate_id,
                original_callable=original_callable,
                carrier=ProductionCarrierState.placeholder(
                    candidate_id, self._manifest_sha256
                ),
            )
            captured = try_capture(CaptureRequest(func))
            record = CandidateRecord(
                candidate_id,
                wrapper,
                capture_ir=captured.capture_ir if captured.supported else None,
            )
            self._records[func_id] = record
            self._func_refs[func_id] = self._weakref_or_none(
                func, lambda _ref, key=func_id: self._drop_func(key)
            )
            self.registration_count += 1
            while len(self._records) > self._max_candidates:
                evicted_id, _ = self._records.popitem(last=False)
                self._func_refs.pop(evicted_id, None)

        events.try_emit(
            DecisionEvent(
                stage="adapter",
                decision="candidate_registered",
                reason_code="compatible_expression_call",
                candidate_id=candidate_id,
            )
        )
        return record

    def bind_expression(self, expression: Any, record: CandidateRecord) -> None:
        expression_id = id(expression)
        with self._lock:
            record.expression_id = expression_id
            self._expression_records[expression_id] = record
            self._expression_refs[expression_id] = self._weakref_or_none(
                expression,
                lambda _ref, key=expression_id: self._drop_expression(key),
            )

    def _records_for_expressions(
        self, expressions: Iterable[Any]
    ) -> tuple[CandidateRecord, ...]:
        found: list[CandidateRecord] = []
        seen: set[str] = set()
        with self._lock:
            for expression in expressions:
                record = self._expression_records.get(id(expression))
                if record is not None and record.candidate_id not in seen:
                    found.append(record)
                    seen.add(record.candidate_id)
        return tuple(found)

    def finalize_columns(self, dataframe: Any, columns: Any) -> int:
        if not isinstance(columns, dict):
            return 0
        records = self._records_for_expressions(columns.values())
        if not records:
            return 0
        logical_schema = repr(dataframe.schema())
        finalized = 0
        for record in records:
            with self._lock:
                if record.finalized:
                    continue
                artifact_bytes: bytes | None = None
                if record.capture_ir is not None and "float64" in logical_schema.lower():
                    try:
                        module = lower_capture(record.capture_ir)
                        artifact_bytes = encode_artifact(
                            build_artifact(
                                module,
                                form_verified_region(module),
                                module.fallback_identity,
                            )
                        )
                    except Exception:
                        artifact_bytes = None
                if not record.wrapper.finalize(
                    logical_schema, "projection", artifact_bytes
                ):
                    continue
                record.finalized = True
                self.finalization_count += 1
                finalized += 1
            events.try_emit(
                DecisionEvent(
                    stage="adapter",
                    decision="operation_finalized",
                    reason_code="with_columns_projection",
                    candidate_id=record.candidate_id,
                )
            )
        return finalized

    def records(self) -> tuple[CandidateRecord, ...]:
        with self._lock:
            return tuple(self._records.values())
