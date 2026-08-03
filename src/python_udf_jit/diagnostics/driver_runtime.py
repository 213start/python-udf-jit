"""Driver-only rejection evidence for candidates that never reach a Worker.

The module is imported lazily only when a full diagnostic policy is bound
to the Driver registry.  Normal execution therefore does not construct source
maps, bytecode documents, provenance, JSON payloads, or bundle writers.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from types import FunctionType
from typing import Any

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.capture import CaptureIR
from python_udf_jit.compiler.identity import code_identity_from_code
from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.bundle import (
    BundleRef,
    BundleRunContext,
    BundleStatus,
    open_bundle,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticPolicySnapshot,
    DiagnosticProfile,
    DiagnosticSourcePolicy,
    canonical_json_bytes,
)
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.diagnostics.provenance import (
    build_bytecode_artifacts,
    build_original_provenance,
    program_source_map_document,
)
from python_udf_jit.diagnostics.session import open_diagnostic_session


_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_DETAIL = re.compile(r"^[A-Za-z0-9_.:-]{0,512}$")
_DOWNSTREAM_REJECTION_REASON = {
    "adapter": "capture_rejected",
    "capture": "capture_rejected",
    "semantic": "semantic_rejected",
    "artifact": "artifact_rejected",
}


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redacted_capture_document(
    capture: CapturedProgram,
) -> dict[str, object]:
    """Keep capture structure while removing reversible scalar values."""

    document = capture.to_document()
    kind_by_index = {
        instruction.argument: instruction.constant_kind or "unknown"
        for instruction in capture.frontend.decoded_bytecode.instructions
        if (
            instruction.operation == "constant.load"
            and instruction.argument is not None
        )
    }
    document["scalar_constants"] = [
        None
        if value is None
        else {
            "kind": kind_by_index.get(index, "unknown"),
            "sha256": _hash_text(value),
        }
        for index, value in enumerate(capture.scalar_constants)
    ]
    return document


def _diagnostic_function(
    callable_object: Any,
    original_callable: Any,
) -> FunctionType:
    """Resolve code without executing user descriptors or the UDF itself."""

    if type(callable_object) is FunctionType:
        return callable_object
    try:
        namespace = object.__getattribute__(callable_object, "__dict__")
    except (AttributeError, TypeError):
        namespace = None
    if type(namespace) is dict:
        method = namespace.get("_method")
        if type(method) is FunctionType:
            wrapped = vars(method).get("__wrapped__")
            if type(wrapped) is FunctionType:
                return wrapped
            return method
    if type(original_callable) is FunctionType:
        wrapped = vars(original_callable).get("__wrapped__")
        if type(wrapped) is FunctionType:
            return wrapped
        return original_callable
    raise TypeError("driver_diagnostic_function_unavailable")


def _safe_reason(value: str, fallback: str) -> str:
    return value if _SAFE_REASON.fullmatch(value) is not None else fallback


def _safe_detail(value: str) -> str:
    if _SAFE_DETAIL.fullmatch(value) is not None:
        return value
    return f"sha256:{_hash_text(value)}"


def _selector_matches(
    selector: str,
    *,
    candidate_id: str,
    code_sha256: str,
) -> bool:
    kind, separator, value = selector.partition(":")
    if not separator or not value:
        raise ValueError("diagnostic_selector_invalid")
    if kind == "candidate":
        return candidate_id == value
    if kind == "udf":
        return code_sha256.startswith(value)
    if kind in {"artifact", "region"}:
        return False
    raise ValueError("diagnostic_selector_unsupported")


@dataclass(frozen=True)
class DriverRejection:
    stage: str
    reason_code: str
    reason_detail: str = ""

    def __post_init__(self) -> None:
        if self.stage not in {"adapter", "capture", "semantic", "artifact"}:
            raise ValueError("driver_rejection_stage_invalid")
        if not self.reason_code:
            raise ValueError("driver_rejection_reason_missing")


class DriverDiagnosticStatus(StrEnum):
    RECORDED = "recorded"
    NOT_SELECTED = "not_selected"
    FAILED = "failed"


@dataclass(frozen=True)
class DriverDiagnosticOutcome:
    status: DriverDiagnosticStatus
    bundle: BundleRef | None = None
    reason_code: str = ""


class DriverDiagnosticRecorder:
    """Publish a source-to-rejection Bundle before Worker serialization."""

    def __init__(
        self,
        policy: DiagnosticPolicySnapshot,
        *,
        run_id: str,
        runtime_mode: str,
        process_key: str,
    ) -> None:
        if not policy.enabled:
            raise ValueError("driver_diagnostics_requires_enabled_policy")
        self._policy = policy
        self._run_context = BundleRunContext(
            run_id,
            runtime_mode,
            process_key,
        )
        self._failure_count = 0
        self._failure_lock = threading.Lock()

    @property
    def failure_count(self) -> int:
        with self._failure_lock:
            return self._failure_count

    def _failed(
        self,
        candidate_id: str,
        reason_code: str,
        session: Any | None,
    ) -> DriverDiagnosticOutcome:
        bundle = None
        if session is not None:
            try:
                bundle = session.finalize(BundleStatus.INCOMPLETE)
            except Exception:
                bundle = None
        with self._failure_lock:
            self._failure_count += 1
        events.try_emit(
            DecisionEvent(
                stage="diagnostics",
                decision="recording_failed",
                reason_code=reason_code,
                candidate_id=candidate_id,
            )
        )
        return DriverDiagnosticOutcome(
            DriverDiagnosticStatus.FAILED,
            bundle,
            reason_code,
        )

    def record_rejection(
        self,
        *,
        candidate_id: str,
        callable_object: Any,
        original_callable: Any,
        logical_schema: str,
        usage_context: str,
        rejection: DriverRejection,
        captured_program: CapturedProgram | CaptureIR | None = None,
    ) -> DriverDiagnosticOutcome:
        if self._policy.profile is not DiagnosticProfile.FULL:
            return DriverDiagnosticOutcome(
                DriverDiagnosticStatus.NOT_SELECTED,
            )
        session = None
        try:
            function = _diagnostic_function(
                callable_object,
                original_callable,
            )
            code_identity = code_identity_from_code(function.__code__)
            if not _selector_matches(
                self._policy.selector,
                candidate_id=candidate_id,
                code_sha256=code_identity.sha256,
            ):
                return DriverDiagnosticOutcome(
                    DriverDiagnosticStatus.NOT_SELECTED,
                )
            writer = open_bundle(self._policy, self._run_context)
            session = open_diagnostic_session(
                self._policy,
                bundle_writer=writer,
            )
            original = build_bytecode_artifacts(
                function.__code__,
                code_hash=code_identity.sha256,
            )
            provenance = build_original_provenance(
                function.__code__,
                code_hash=code_identity.sha256,
            )
            source_ranges = program_source_map_document(provenance)
            capture = (
                captured_program.program
                if isinstance(captured_program, CaptureIR)
                else captured_program
            )
            source_text = None
            source_text_missing = False
            if (
                self._policy.source_policy
                is DiagnosticSourcePolicy.TEXT
            ):
                try:
                    source_text = inspect.getsource(function)
                except (OSError, TypeError):
                    source_text_missing = True
            reason_code = _safe_reason(
                rejection.reason_code,
                f"{rejection.stage}_rejected",
            )
            reason_detail = _safe_detail(rejection.reason_detail)
            capture_unavailable = (
                reason_code
                if rejection.stage in {"adapter", "capture"}
                else None
            )
            capture_evidence_missing = (
                rejection.stage in {"semantic", "artifact"}
                and capture is None
            )
            downstream_reason = _DOWNSTREAM_REJECTION_REASON[rejection.stage]
            identity = {
                "code_sha256": code_identity.sha256,
                "module_sha256": _hash_text(function.__module__),
                "qualname_sha256": _hash_text(function.__qualname__),
                "schema_version": 1,
            }
            signature = {
                "candidate_id": candidate_id,
                "cellvars_count": len(function.__code__.co_cellvars),
                "code_sha256": code_identity.sha256,
                "flags": function.__code__.co_flags,
                "freevars_count": len(function.__code__.co_freevars),
                "has_varargs": bool(function.__code__.co_flags & inspect.CO_VARARGS),
                "has_varkwargs": bool(
                    function.__code__.co_flags & inspect.CO_VARKEYWORDS
                ),
                "logical_schema_sha256": _hash_text(logical_schema),
                "positional_args": function.__code__.co_argcount,
                "positional_only_args": function.__code__.co_posonlyargcount,
                "keyword_only_args": function.__code__.co_kwonlyargcount,
                "schema_version": 1,
                "usage_context": usage_context,
            }
            capture_result = {
                "admitted": capture is not None,
                "candidate_id": candidate_id,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "schema_version": 1,
                "stage": rejection.stage,
            }
            chain_status = {
                "adapter": {
                    "status": (
                        "rejected"
                        if rejection.stage == "adapter"
                        else "available"
                    ),
                    "unavailable_reason": (
                        reason_code
                        if rejection.stage == "adapter"
                        else None
                    ),
                },
                "capture": {
                    "status": (
                        "rejected"
                        if rejection.stage == "capture"
                        else "unavailable"
                        if rejection.stage == "adapter"
                        else "available"
                        if capture is not None
                        else "unavailable"
                    ),
                    "unavailable_reason": (
                        capture_unavailable
                        if rejection.stage in {"adapter", "capture"}
                        else None
                        if capture is not None
                        else "capture_evidence_missing"
                    ),
                },
                "hir": {
                    "status": "unavailable",
                    "unavailable_reason": downstream_reason,
                },
                "lir": {
                    "status": "unavailable",
                    "unavailable_reason": downstream_reason,
                },
                "machine": {
                    "status": "unavailable",
                    "unavailable_reason": downstream_reason,
                },
                "original_bytecode": "available",
                "schema_version": 1,
                "semantic": {
                    "status": (
                        "rejected"
                        if rejection.stage == "semantic"
                        else "available"
                        if rejection.stage == "artifact"
                        else "unavailable"
                    ),
                    "unavailable_reason": (
                        reason_code
                        if rejection.stage == "semantic"
                        else None
                        if rejection.stage == "artifact"
                        else downstream_reason
                    ),
                },
                "source": "available",
                "source_text": {
                    "status": (
                        "available"
                        if source_text is not None
                        else "unavailable"
                    ),
                    "unavailable_reason": (
                        None
                        if source_text is not None
                        else "source_policy_ranges"
                        if (
                            self._policy.source_policy
                            is DiagnosticSourcePolicy.RANGES
                        )
                        else "source_text_unavailable"
                    ),
                },
            }
            nodes = {
                "format_version": provenance.format_version,
                "nodes": [node.to_document() for node in provenance.nodes],
            }
            edges = {
                "edges": [edge.to_document() for edge in provenance.edges],
                "format_version": provenance.format_version,
            }
            artifacts: list[tuple[str, object, str]] = [
                ("source/identity.json", identity, "source"),
                ("source/ranges.json", source_ranges, "source"),
                ("candidate/signature.json", signature, "candidate"),
                ("bytecode/original.json", original.json_document, "bytecode"),
                ("capture/result.json", capture_result, "capture"),
                ("provenance/map.json", provenance.to_document(), "provenance"),
                ("provenance/nodes.json", nodes, "provenance"),
                ("provenance/edges.json", edges, "provenance"),
            ]
            if capture is not None:
                artifacts.extend(
                    (
                        (
                            "capture/capture.json",
                            _redacted_capture_document(capture),
                            "capture",
                        ),
                        (
                            "capture/cfg.json",
                            capture.frontend.control_flow_graph.to_document(),
                            "capture",
                        ),
                    )
                )
            required_artifact_missing = False
            for path, payload, layer in artifacts:
                if (
                    session.record_artifact(
                        path,
                        "application/json",
                        canonical_json_bytes(payload),
                        {"layer": layer},
                    )
                    is None
                ):
                    required_artifact_missing = True
            if (
                session.record_artifact(
                    "bytecode/original.dis",
                    "text/plain",
                    original.disassembly,
                    {"layer": "bytecode"},
                )
                is None
            ):
                required_artifact_missing = True
            if source_text is not None and (
                session.record_artifact(
                    "source/source.py",
                    "text/x-python",
                    source_text,
                    {"layer": "source"},
                )
                is None
            ):
                source_text_missing = True
                chain_status["source_text"] = {
                    "status": "unavailable",
                    "unavailable_reason": "source_text_unavailable",
                }
            if not required_artifact_missing and (
                session.record_artifact(
                    "reports/chain-status.json",
                    "application/json",
                    canonical_json_bytes(chain_status),
                    {"layer": "reports"},
                )
                is None
            ):
                required_artifact_missing = True
            if required_artifact_missing:
                return self._failed(
                    candidate_id,
                    "driver_required_artifact_missing",
                    session,
                )
            if capture_evidence_missing:
                return self._failed(
                    candidate_id,
                    "driver_capture_evidence_missing",
                    session,
                )
            if source_text_missing:
                return self._failed(
                    candidate_id,
                    "driver_source_text_unavailable",
                    session,
                )
            bundle = session.finalize(BundleStatus.PARTIAL)
            if bundle is None:
                return self._failed(
                    candidate_id,
                    "driver_bundle_finalize_failed",
                    None,
                )
            return DriverDiagnosticOutcome(
                DriverDiagnosticStatus.RECORDED,
                bundle,
            )
        except Exception:
            return self._failed(
                candidate_id,
                "driver_recording_failed",
                session,
            )
