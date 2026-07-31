"""Dedicated-worker binding for diagnostic compilation evidence.

This module is imported only after a frozen non-off diagnostic policy has
matched the current carrier.  Normal workers therefore never initialize a
bundle, provenance recorder, clock, or CinderX diagnostic observer.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from types import FunctionType

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.diagnostics.bundle import (
    BundleRunContext,
    BundleStatus,
    open_bundle,
)
from python_udf_jit.diagnostics.cinderx_bridge import (
    build_cinderx_artifacts,
    collect_cinderx_compilation_diagnostics,
    extend_provenance_with_cinderx,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticPerfMode,
    DiagnosticPolicySnapshot,
    DiagnosticProfile,
    canonical_json_bytes,
)
from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile
from python_udf_jit.diagnostics.provenance import (
    ProvenanceMap,
    UpperProvenanceRecorder,
    build_bytecode_artifacts,
)
from python_udf_jit.diagnostics.session import open_diagnostic_session
from python_udf_jit.protocol.artifact import PortableUdfArtifact


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selector_matches(
    selector: str,
    *,
    candidate_id: str,
    artifact_sha256: str,
) -> bool:
    kind, separator, value = selector.partition(":")
    if not separator or not value:
        raise ValueError("diagnostic_selector_invalid")
    if kind == "artifact":
        return artifact_sha256.startswith(value)
    if kind == "candidate":
        return candidate_id == value
    if kind in {"udf", "region"}:
        # These identities become available after artifact re-verification.
        return True
    raise ValueError("diagnostic_selector_unsupported")


@dataclass
class _CompilationState:
    recorder: UpperProvenanceRecorder
    original_json: dict[str, object]
    original_text: str


class _SafeProvenanceSink:
    def __init__(
        self,
        runtime: "WorkerDiagnosticRuntime",
        recorder: UpperProvenanceRecorder,
    ) -> None:
        self._runtime = runtime
        self._recorder = recorder

    def record_scalar_lowering(self, snapshot) -> None:
        try:
            self._recorder.record_scalar_lowering(snapshot)
        except Exception:
            self._runtime.mark_partial()


class WorkerDiagnosticRuntime:
    """One immutable summary/full session for a matched diagnostic carrier."""

    def __init__(
        self,
        policy: DiagnosticPolicySnapshot,
        *,
        run_id: str,
        runtime_mode: str,
        process_key: str,
        process_id: int,
        user_function: FunctionType,
    ) -> None:
        if not policy.enabled:
            raise ValueError("worker_diagnostics_requires_enabled_policy")
        if type(process_id) is not int or process_id <= 0:
            raise ValueError("worker_diagnostic_process_id_invalid")
        writer = open_bundle(
            policy,
            BundleRunContext(run_id, runtime_mode, process_key),
        )
        self.policy = policy
        self._run_id = run_id
        self._process_id = process_id
        self._selector = policy.selector
        self._user_function = user_function
        self._session = open_diagnostic_session(
            policy,
            bundle_writer=writer,
        )
        self._states: dict[str, _CompilationState] = {}
        self._analysis = None
        self._partial = False
        self._selected = policy.selector.startswith(
            ("artifact:", "candidate:")
        )
        self._perf_recorded = False
        self._finalized = False
        self._lock = threading.Lock()

    def span(self, stage: str, identity: str = ""):
        return self._session.span(stage, identity)

    def mark_partial(self) -> None:
        with self._lock:
            self._partial = True

    def provenance_sink(
        self,
        artifact: PortableUdfArtifact,
        key,
    ):
        selector_kind, _, selector_value = self._selector.partition(":")
        if selector_kind == "udf":
            self._selected = (
                artifact.fallback_identity.code_sha256.startswith(
                    selector_value
                )
                or artifact.semantic_core_module.function_id.startswith(
                    selector_value
                )
            )
        elif selector_kind == "region":
            self._selected = any(
                region.region_id == selector_value
                or artifact.semantic_region_graph.semantic_hash.startswith(
                    selector_value
                )
                for region in artifact.semantic_region_graph.regions
            )
        if (
            not self._selected
            or self.policy.profile is not DiagnosticProfile.FULL
        ):
            return None
        try:
            with self._lock:
                if self._analysis is None:
                    identities = capture_identities(self._user_function)
                    program = analyze_function(
                        self._user_function,
                        identities=identities,
                    )
                    self._analysis = (identities, program)
                else:
                    identities, program = self._analysis
            if (
                identities.code.sha256
                != artifact.fallback_identity.code_sha256
                or identities.source.code_sha256
                != artifact.semantic_core_module.function_id
            ):
                raise ValueError("diagnostic_source_identity_mismatch")
            recorder = UpperProvenanceRecorder(
                program.frontend.source_map,
                artifact.semantic_core_module,
                artifact.semantic_region_graph,
            )
            original = build_bytecode_artifacts(
                self._user_function.__code__,
                code_hash=artifact.fallback_identity.code_sha256,
            )
            with self._lock:
                self._states[key.sha256] = _CompilationState(
                    recorder,
                    original.json_document,
                    original.disassembly,
                )
            return _SafeProvenanceSink(self, recorder)
        except Exception:
            self.mark_partial()
            return None

    def prepare_compilation(self, compiled, key) -> str:
        if (
            not self._selected
            or self.policy.profile is not DiagnosticProfile.FULL
        ):
            return ""
        compile_instance_id = f"compile-{key.sha256[:24]}"
        try:
            setattr(
                compiled.jit_function,
                "__udfjit_generated_code_hash__",
                compiled.code_hash,
            )
        except Exception:
            self.mark_partial()
            return ""
        return compile_instance_id

    def _artifact(
        self,
        path: str,
        media_type: str,
        payload: object,
        *,
        layer: str,
    ) -> bool:
        encoded = (
            payload
            if isinstance(payload, (bytes, str))
            else canonical_json_bytes(payload)
        )
        if (
            self._session.record_artifact(
                path,
                media_type,
                encoded,
                {"layer": layer},
            )
            is None
        ):
            self.mark_partial()
            return False
        return True

    def record_compilation(
        self,
        jit_module: object,
        compiled,
        key,
        compile_instance_id: str,
    ) -> None:
        if self.policy.profile is not DiagnosticProfile.FULL:
            return
        try:
            diagnostics = collect_cinderx_compilation_diagnostics(
                jit_module,
                compiled.jit_function,
                compile_instance_id=compile_instance_id,
                generated_code_hash=compiled.code_hash,
            )
            with self._lock:
                state = self._states.get(key.sha256)
            if state is None:
                self.mark_partial()
                return
            recorder = state.recorder
            base_provenance = recorder.provenance_map
            provenance = extend_provenance_with_cinderx(
                base_provenance,
                diagnostics,
            )
            cinderx = build_cinderx_artifacts(diagnostics)
            generated = recorder.generated_bytecode_artifacts
            if generated is None:
                self.mark_partial()
                return
            semantic = recorder.semantic_artifacts
            identity = {
                "code_sha256": state.original_json["code_hash"],
                "module_sha256": _hash_text(self._user_function.__module__),
                "qualname_sha256": _hash_text(
                    self._user_function.__qualname__
                ),
                "schema_version": 1,
            }
            artifacts = (
                ("source/identity.json", "application/json", identity, "source"),
                (
                    "source/ranges.json",
                    "application/json",
                    program_source_map_document(base_provenance),
                    "source",
                ),
                (
                    "bytecode/original.json",
                    "application/json",
                    state.original_json,
                    "bytecode",
                ),
                (
                    "bytecode/original.dis",
                    "text/plain",
                    state.original_text,
                    "bytecode",
                ),
                (
                    "semantic/core.final.json",
                    "application/json",
                    semantic.core_json,
                    "semantic",
                ),
                (
                    "semantic/core.final.txt",
                    "text/plain",
                    semantic.core_text,
                    "semantic",
                ),
                (
                    "semantic/regions.json",
                    "application/json",
                    semantic.regions_json,
                    "region",
                ),
                (
                    "semantic/regions.txt",
                    "text/plain",
                    semantic.regions_text,
                    "region",
                ),
                (
                    "lowering/generated_ast.txt",
                    "text/plain",
                    recorder.generated_ast_text,
                    "lowering",
                ),
                (
                    "lowering/lowering-map.json",
                    "application/json",
                    recorder.lowering_map,
                    "lowering",
                ),
                (
                    "bytecode/generated.json",
                    "application/json",
                    generated.json_document,
                    "bytecode",
                ),
                (
                    "bytecode/generated.dis",
                    "text/plain",
                    generated.disassembly,
                    "bytecode",
                ),
                (
                    "cinderx/hir.final.json",
                    "application/json",
                    cinderx.hir_json,
                    "hir",
                ),
                (
                    "cinderx/hir.final.txt",
                    "text/plain",
                    cinderx.hir_text,
                    "hir",
                ),
                (
                    "cinderx/lir-origin.json",
                    "application/json",
                    cinderx.lir_json,
                    "lir",
                ),
                (
                    "cinderx/lir.txt",
                    "text/plain",
                    cinderx.lir_text,
                    "lir",
                ),
                (
                    "cinderx/machine-ranges.json",
                    "application/json",
                    cinderx.machine_ranges_json,
                    "machine",
                ),
                (
                    "cinderx/machine-ranges.txt",
                    "text/plain",
                    cinderx.machine_ranges_text,
                    "machine",
                ),
                (
                    "cinderx/compile-stats.json",
                    "application/json",
                    cinderx.compile_stats_json,
                    "cinderx",
                ),
                (
                    "provenance/map.json",
                    "application/json",
                    provenance.to_document(),
                    "provenance",
                ),
                (
                    "provenance/nodes.json",
                    "application/json",
                    {
                        "format_version": provenance.format_version,
                        "nodes": [
                            node.to_document() for node in provenance.nodes
                        ],
                    },
                    "provenance",
                ),
                (
                    "provenance/edges.json",
                    "application/json",
                    {
                        "edges": [
                            edge.to_document() for edge in provenance.edges
                        ],
                        "format_version": provenance.format_version,
                    },
                    "provenance",
                ),
            )
            for path, media_type, payload, layer in artifacts:
                self._artifact(
                    path,
                    media_type,
                    payload,
                    layer=layer,
                )
            for timing in diagnostics.pass_timings:
                self._session.record_metric(
                    f"cinderx_pass_{timing.ordinal}",
                    timing.duration_ns,
                    timing.name,
                )
            if not diagnostics.jit_compiled:
                self.mark_partial()
        except Exception:
            self.mark_partial()

    def record_perf_profile(
        self,
        profile: NormalizedPerfProfile,
        *,
        raw_perf_data: bytes | None = None,
    ) -> bool:
        if (
            self.policy.perf_mode is not DiagnosticPerfMode.RECORD
            or not isinstance(profile, NormalizedPerfProfile)
            or profile.run_id != self._run_id
            or profile.process_id != self._process_id
            or (
                raw_perf_data is not None
                and not isinstance(raw_perf_data, bytes)
            )
        ):
            self.mark_partial()
            return False
        try:
            samples_recorded = self._artifact(
                "perf/samples.json",
                "application/json",
                profile.to_document(),
                layer="perf",
            )
            self._perf_recorded = samples_recorded
            raw_recorded = True
            if (
                raw_perf_data is not None
                and self.policy.perf_mode is DiagnosticPerfMode.RECORD
            ):
                raw_recorded = self._artifact(
                    "perf/perf.data",
                    "application/octet-stream",
                    raw_perf_data,
                    layer="perf",
                )
            return samples_recorded and raw_recorded
        except Exception:
            self.mark_partial()
            return False

    def finalize(self):
        with self._lock:
            if self._finalized:
                return None
            self._finalized = True
            status = (
                BundleStatus.PARTIAL
                if (
                    self._partial
                    or (
                        self.policy.perf_mode is DiagnosticPerfMode.RECORD
                        and not self._perf_recorded
                    )
                )
                else BundleStatus.COMPLETE
            )
        return self._session.finalize(status)


def program_source_map_document(
    provenance: ProvenanceMap,
) -> dict[str, object]:
    source_nodes = [
        node.to_document()
        for node in provenance.nodes
        if node.layer.value == "source"
    ]
    return {"format_version": 1, "ranges": source_nodes}


def open_worker_diagnostic_runtime(
    policy: DiagnosticPolicySnapshot,
    *,
    run_id: str,
    runtime_mode: str,
    process_key: str,
    process_id: int,
    candidate_id: str,
    artifact_sha256: str,
    user_function: FunctionType,
) -> WorkerDiagnosticRuntime | None:
    if not policy.enabled:
        raise ValueError("worker_diagnostics_requires_enabled_policy")
    if not _selector_matches(
        policy.selector,
        candidate_id=candidate_id,
        artifact_sha256=artifact_sha256,
    ):
        return None
    return WorkerDiagnosticRuntime(
        policy,
        run_id=run_id,
        runtime_mode=runtime_mode,
        process_key=process_key,
        process_id=process_id,
        user_function=user_function,
    )
