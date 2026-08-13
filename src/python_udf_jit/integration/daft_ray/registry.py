from __future__ import annotations

import hashlib
import pickle
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.capture import (
    CaptureIR,
    CaptureRejectCode,
    CaptureRejected,
    CaptureRequest,
    capture_program_request,
    fallback_identity_for_program,
    try_capture,
)
from python_udf_jit.compiler.capture_cache import CaptureCache
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.config import (
    DiagnosticPolicySnapshot,
    DiagnosticProfile,
    OFF_DIAGNOSTIC_POLICY,
)
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.governance.policy import (
    DEFAULT_MAINLINE_POLICY,
    PolicySnapshot,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.schema import canonicalize_schema
from python_udf_jit.integration.daft_ray.wrapper import (
    BatchExecutionWrapper,
    FallbackOnlyWrapper,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact


_MAX_EXPRESSION_LINEAGE_BYTES = 1024 * 1024


class _BoundedPickleSink:
    """Capture framework expression state without allowing unbounded growth."""

    def __init__(self, limit: int):
        self._limit = limit
        self._payload = bytearray()

    def write(self, payload: bytes) -> int:
        if len(self._payload) + len(payload) > self._limit:
            raise ValueError("expression_lineage_size_limit")
        self._payload.extend(payload)
        return len(payload)

    def getvalue(self) -> bytes:
        return bytes(self._payload)


@dataclass
class CandidateRecord:
    registry_key: int
    func_id: int
    candidate_id: str
    wrapper: FallbackOnlyWrapper
    batch_wrapper: BatchExecutionWrapper | None
    capture_callable: Any
    job_namespace: str
    expires_at: float
    expression_ids: set[int] = field(default_factory=set)
    pyexpr_hashes: set[int] = field(default_factory=set)
    finalized: bool = False
    capture_ir: CaptureIR | None = None
    semantic_core_hash: str | None = None
    semantic_region_hash: str | None = None


def _candidate_id(
    func: Any,
    original_callable: Callable[..., Any],
    job_namespace: str,
    registry_key: int,
) -> str:
    code = getattr(original_callable, "__code__", None)
    code_bytes = getattr(code, "co_code", b"")
    identity = "\0".join(
        (
            job_namespace,
            str(registry_key),
            str(getattr(func, "func_id", "")),
            str(getattr(original_callable, "__module__", "")),
            str(getattr(original_callable, "__qualname__", "")),
        )
    ).encode("utf-8") + bytes(code_bytes)
    return hashlib.sha256(identity).hexdigest()


class CandidateRegistry:
    """Bounded, process-local registry; it is never serialized to a Worker."""

    def __init__(
        self,
        manifest_sha256: str,
        max_candidates: int = 1024,
        *,
        job_namespace: str = "local-test-job",
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        policy: PolicySnapshot = DEFAULT_MAINLINE_POLICY,
        diagnostic_policy: DiagnosticPolicySnapshot = OFF_DIAGNOSTIC_POLICY,
        diagnostic_run_id: str = "driver-diagnostic",
        diagnostic_runtime_mode: str = "observe",
        diagnostic_process_key: str = "driver",
    ):
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if not isinstance(job_namespace, str) or not job_namespace:
            raise ValueError("job_namespace must not be empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not isinstance(policy, PolicySnapshot):
            raise ValueError("policy snapshot is required")
        if not isinstance(diagnostic_policy, DiagnosticPolicySnapshot):
            raise ValueError("diagnostic policy snapshot is required")
        self._manifest_sha256 = manifest_sha256
        self._max_candidates = max_candidates
        self._job_namespace = job_namespace
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._policy = policy
        self._diagnostic_policy = diagnostic_policy
        self._driver_diagnostics = None
        if diagnostic_policy.profile is DiagnosticProfile.FULL:
            from python_udf_jit.diagnostics.driver_runtime import (
                DriverDiagnosticRecorder,
            )

            self._driver_diagnostics = DriverDiagnosticRecorder(
                diagnostic_policy,
                run_id=diagnostic_run_id,
                runtime_mode=diagnostic_runtime_mode,
                process_key=diagnostic_process_key,
            )
        self._capture_cache = CaptureCache(
            capacity=max_candidates,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )
        self._records: OrderedDict[int, CandidateRecord] = OrderedDict()
        self._func_records: dict[int, CandidateRecord] = {}
        self._func_refs: dict[int, weakref.ReferenceType[Any] | None] = {}
        self._expression_records: dict[int, CandidateRecord] = {}
        self._pyexpr_records: dict[int, CandidateRecord] = {}
        self._expression_refs: dict[int, weakref.ReferenceType[Any] | None] = {}
        self._expression_hashes: dict[int, int] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._next_registry_key = 0
        self.registration_count = 0
        self.finalization_count = 0

    def _drop_record_locked(self, registry_key: int) -> CandidateRecord | None:
        record = self._records.pop(registry_key, None)
        if record is None:
            return record
        if self._func_records.get(record.func_id) is record:
            self._func_records.pop(record.func_id, None)
            self._func_refs.pop(record.func_id, None)
        for expression_id in tuple(record.expression_ids):
            if self._expression_records.get(expression_id) is record:
                self._expression_records.pop(expression_id, None)
            self._expression_refs.pop(expression_id, None)
            self._expression_hashes.pop(expression_id, None)
        for pyexpr_hash in tuple(record.pyexpr_hashes):
            if self._pyexpr_records.get(pyexpr_hash) is record:
                self._pyexpr_records.pop(pyexpr_hash, None)
        record.expression_ids.clear()
        record.pyexpr_hashes.clear()
        return record

    def _drop_func(self, func_id: int) -> None:
        with self._lock:
            record = self._func_records.get(func_id)
            if record is not None and not record.expression_ids:
                self._drop_record_locked(record.registry_key)

    def _drop_expression(self, expression_id: int) -> None:
        with self._lock:
            record = self._expression_records.pop(expression_id, None)
            self._expression_refs.pop(expression_id, None)
            pyexpr_hash = self._expression_hashes.pop(expression_id, None)
            if record is None:
                return
            record.expression_ids.discard(expression_id)
            if pyexpr_hash is not None:
                record.pyexpr_hashes.discard(pyexpr_hash)
                if self._pyexpr_records.get(pyexpr_hash) is record:
                    self._pyexpr_records.pop(pyexpr_hash, None)
            func_ref = self._func_refs.get(record.func_id)
            if (
                not record.expression_ids
                and func_ref is not None
                and func_ref() is None
            ):
                self._drop_record_locked(record.registry_key)

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
            self._assert_open()
            self._purge_expired_locked()
            existing = self._func_records.get(func_id)
            func_ref = self._func_refs.get(func_id)
            if (
                existing is not None
                and func_ref is not None
                and func_ref() is not func
            ):
                if existing.expression_ids:
                    self._func_records.pop(func_id, None)
                    self._func_refs.pop(func_id, None)
                else:
                    self._drop_record_locked(existing.registry_key)
                existing = None
            if existing is not None:
                self._records.move_to_end(existing.registry_key)
                existing.expires_at = self._clock() + self._ttl_seconds
                return existing

            self._next_registry_key += 1
            registry_key = self._next_registry_key
            candidate_id = _candidate_id(
                func,
                original_callable,
                self._job_namespace,
                registry_key,
            )
            wrapper = FallbackOnlyWrapper(
                candidate_id=candidate_id,
                original_callable=original_callable,
                carrier=ProductionCarrierState.placeholder(
                    candidate_id,
                    self._manifest_sha256,
                    policy=self._policy,
                    diagnostic_policy=self._diagnostic_policy,
                ),
            )
            record = CandidateRecord(
                registry_key=registry_key,
                func_id=func_id,
                candidate_id=candidate_id,
                wrapper=wrapper,
                batch_wrapper=None,
                capture_callable=func,
                job_namespace=self._job_namespace,
                expires_at=self._clock() + self._ttl_seconds,
            )
            self._records[record.registry_key] = record
            self._func_records[func_id] = record
            self._func_refs[func_id] = self._weakref_or_none(
                func, lambda _ref, key=func_id: self._drop_func(key)
            )
            self.registration_count += 1
            while len(self._records) > self._max_candidates:
                evicted_key = next(iter(self._records))
                self._drop_record_locked(evicted_key)

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
            self._assert_open()
            if self._records.get(record.registry_key) is not record:
                return
            record.expires_at = self._clock() + self._ttl_seconds
            pyexpr_hash = self._pyexpr_hash(expression)
            record.expression_ids.add(expression_id)
            self._expression_records[expression_id] = record
            if pyexpr_hash is not None:
                record.pyexpr_hashes.add(pyexpr_hash)
                self._pyexpr_records[pyexpr_hash] = record
                self._expression_hashes[expression_id] = pyexpr_hash
            self._expression_refs[expression_id] = self._weakref_or_none(
                expression,
                lambda _ref, key=expression_id: self._drop_expression(key),
            )

    @staticmethod
    def _pyexpr_hash(expression: Any) -> int | None:
        try:
            namespace = object.__getattribute__(expression, "__dict__")
        except (AttributeError, TypeError):
            return None
        if type(namespace) is not dict:
            return None
        pyexpr = namespace.get("_expr")
        hasher = getattr(pyexpr, "_hash", None)
        if not callable(hasher):
            return None
        try:
            value = hasher()
        except Exception:
            return None
        return value if type(value) is int else None

    @staticmethod
    def _walk_values(
        roots: tuple[Any, ...],
        *,
        max_nodes: int = 4096,
        max_depth: int = 32,
    ) -> tuple[Any, ...]:
        pending = [(root, 0) for root in roots]
        found: list[Any] = []
        visited: set[int] = set()
        while pending:
            value, depth = pending.pop()
            if depth > max_depth:
                raise ValueError("operation_expression_depth_limit")
            identity = id(value)
            if identity in visited:
                continue
            visited.add(identity)
            if len(visited) > max_nodes:
                raise ValueError("operation_expression_node_limit")
            found.append(value)
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.keys())
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend((item, depth + 1) for item in value)
        return tuple(found)

    def _records_for_expressions(
        self,
        expressions: tuple[Any, ...],
    ) -> tuple[CandidateRecord, ...]:
        found: list[CandidateRecord] = []
        seen: set[int] = set()
        for expression in expressions:
            pyexpr_hash = self._pyexpr_hash(expression)
            with self._lock:
                record = self._expression_records.get(id(expression))
                if record is None and pyexpr_hash is not None:
                    record = self._pyexpr_records.get(pyexpr_hash)
                if record is not None and record.registry_key not in seen:
                    found.append(record)
                    seen.add(record.registry_key)
                    continue
            try:
                sink = _BoundedPickleSink(_MAX_EXPRESSION_LINEAGE_BYTES)
                pickle.Pickler(sink, protocol=5).dump(expression)
                payload = sink.getvalue()
            except Exception:
                continue
            with self._lock:
                for candidate in self._records.values():
                    if (
                        candidate.registry_key not in seen
                        and candidate.candidate_id.encode("ascii") in payload
                    ):
                        found.append(candidate)
                        seen.add(candidate.registry_key)
        return tuple(found)

    def finalize_operation(
        self,
        dataframe: Any,
        operation: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> int:
        contexts = {
            "where": "filter",
            "select": "selection",
            "with_columns": "projection",
        }
        context = contexts.get(operation)
        if context is None:
            raise ValueError("operation_unsupported")
        with self._lock:
            self._assert_open()
            self._purge_expired_locked()
        expressions = self._walk_values((*args, kwargs))
        records = self._records_for_expressions(expressions)
        if not records:
            return 0
        logical_schema = canonicalize_schema(dataframe.schema())
        finalized = 0
        record_driver_diagnostics = self._driver_diagnostics is not None
        for record in records:
            diagnostic_rejection: tuple[str, str, str] | None = None
            with self._lock:
                if record.finalized:
                    continue
                artifact_bytes: bytes | None = None
                semantic_source: CaptureIR | CapturedProgram | None = None
                fallback_identity = None
                if record_driver_diagnostics:
                    diagnostic_rejection = (
                        "adapter",
                        "logical_schema_not_float64",
                        "",
                    )
                if "float64" in logical_schema.lower():
                    try:
                        request = CaptureRequest(
                            record.capture_callable,
                            job_namespace=self._job_namespace,
                            schema_sha256=hashlib.sha256(
                                logical_schema.encode("utf-8")
                            ).hexdigest(),
                            adapter_abi_sha256=hashlib.sha256(
                                b"daft-0.7.2-scalar-capture"
                            ).hexdigest(),
                            policy_sha256=hashlib.sha256(
                                b"python-3.14.3-float64-scalar"
                            ).hexdigest(),
                            capture_cache=self._capture_cache,
                        )
                        captured = try_capture(request)
                        record.capture_ir = (
                            captured.capture_ir if captured.supported else None
                        )
                        if record.capture_ir is not None:
                            semantic_source = record.capture_ir
                            fallback_identity = (
                                record.capture_ir.fallback_identity
                            )
                            diagnostic_rejection = None
                        else:
                            if record_driver_diagnostics:
                                reject_code = (
                                    captured.reject_code.value
                                    if captured.reject_code is not None
                                    else "capture_rejected"
                                )
                                diagnostic_rejection = (
                                    "capture",
                                    reject_code,
                                    captured.reject_detail,
                                )
                            if captured.reject_code in {
                                CaptureRejectCode.OPAQUE_CALL,
                                CaptureRejectCode.GLOBAL_DEPENDENCY,
                            }:
                                program = capture_program_request(request)
                                semantic_source = program
                                fallback_identity = (
                                    fallback_identity_for_program(
                                        record.capture_callable,
                                        program,
                                    )
                                )
                                diagnostic_rejection = None
                    except CaptureRejected as error:
                        record.capture_ir = None
                        semantic_source = None
                        fallback_identity = None
                        if record_driver_diagnostics:
                            diagnostic_rejection = (
                                "capture",
                                error.code.value,
                                error.detail,
                            )
                    except Exception as error:
                        record.capture_ir = None
                        semantic_source = None
                        fallback_identity = None
                        if record_driver_diagnostics:
                            diagnostic_rejection = (
                                "capture",
                                "capture_internal_error",
                                type(error).__name__,
                            )
                if semantic_source is not None and fallback_identity is not None:
                    try:
                        semantic = compile_semantic(semantic_source)
                        closed_scalar = (
                            semantic.reason_code == "verified_semantic_ir"
                            and semantic.region_graph is not None
                            and len(semantic.region_graph.regions) == 1
                            and semantic.region_graph.regions[
                                0
                            ].provider_candidates
                            == ("scalar_cinderx",)
                        )
                        graph_break_scalar = (
                            semantic.reason_code
                            == "verified_scalar_graph_break"
                            and semantic.core_module is not None
                            and len(
                                semantic.core_module.python_regions
                            )
                            == 1
                            and semantic.region_graph is not None
                            and len(semantic.region_graph.regions) == 3
                            and semantic.region_graph.regions[
                                0
                            ].provider_candidates
                            == ("scalar_cinderx",)
                            and not semantic.region_graph.regions[
                                1
                            ].provider_candidates
                            and semantic.region_graph.regions[
                                2
                            ].provider_candidates
                            == ("scalar_cinderx",)
                        )
                        if (
                            not semantic.accepted
                            or semantic.core_module is None
                            or semantic.region_graph is None
                            or not (
                                closed_scalar
                                or graph_break_scalar
                            )
                        ):
                            if record_driver_diagnostics:
                                if (
                                    semantic.accepted
                                    and semantic.core_module is not None
                                    and semantic.region_graph is not None
                                ):
                                    reason_code = (
                                        "semantic_pipeline_not_scalar_eligible"
                                    )
                                else:
                                    reason_code = (
                                        semantic.reason_code
                                        or "semantic_pipeline_rejected"
                                    )
                                diagnostic_rejection = (
                                    "semantic",
                                    reason_code,
                                    "",
                                )
                        else:
                            record.semantic_core_hash = (
                                semantic.core_module.semantic_hash
                            )
                            record.semantic_region_hash = (
                                semantic.region_graph.semantic_hash
                            )
                            try:
                                artifact_bytes = encode_artifact(
                                    build_artifact(
                                        semantic.core_module,
                                        semantic.region_graph,
                                        fallback_identity,
                                    )
                                )
                            except Exception as error:
                                artifact_bytes = None
                                if record_driver_diagnostics:
                                    diagnostic_rejection = (
                                        "artifact",
                                        "artifact_encoding_failed",
                                        type(error).__name__,
                                    )
                            else:
                                diagnostic_rejection = None
                    except Exception as error:
                        artifact_bytes = None
                        if record_driver_diagnostics:
                            diagnostic_rejection = (
                                "semantic",
                                "semantic_pipeline_failed",
                                type(error).__name__,
                            )
                if not record.wrapper.finalize(
                    logical_schema, context, artifact_bytes
                ):
                    continue
                record.finalized = True
                self.finalization_count += 1
                finalized += 1
            if (
                self._driver_diagnostics is not None
                and diagnostic_rejection is not None
            ):
                from python_udf_jit.diagnostics.driver_runtime import (
                    DriverRejection,
                )

                stage, reason_code, reason_detail = diagnostic_rejection
                self._driver_diagnostics.record_rejection(
                    candidate_id=record.candidate_id,
                    callable_object=record.capture_callable,
                    original_callable=record.wrapper.original_callable,
                    logical_schema=logical_schema,
                    usage_context=context,
                    rejection=DriverRejection(
                        stage,
                        reason_code,
                        reason_detail,
                    ),
                    captured_program=semantic_source,
                )
            events.try_emit(
                DecisionEvent(
                    stage="adapter",
                    decision="operation_finalized",
                    reason_code=f"{operation}_{context}",
                    candidate_id=record.candidate_id,
                )
            )
        return finalized

    @property
    def diagnostic_failure_count(self) -> int:
        recorder = self._driver_diagnostics
        return 0 if recorder is None else recorder.failure_count

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("candidate_registry_closed")

    def _purge_expired_locked(self) -> int:
        now = self._clock()
        expired = [
            registry_key
            for registry_key, record in self._records.items()
            if record.expires_at <= now
        ]
        for registry_key in expired:
            self._drop_record_locked(registry_key)
        return len(expired)

    def purge_expired(self) -> int:
        with self._lock:
            self._assert_open()
            return self._purge_expired_locked()

    def close(self) -> None:
        with self._lock:
            self._capture_cache.clear_job(self._job_namespace)
            self._records.clear()
            self._func_records.clear()
            self._func_refs.clear()
            self._expression_records.clear()
            self._expression_refs.clear()
            self._expression_hashes.clear()
            self._pyexpr_records.clear()
            self._closed = True

    def records(self) -> tuple[CandidateRecord, ...]:
        with self._lock:
            self._assert_open()
            self._purge_expired_locked()
            return tuple(self._records.values())
