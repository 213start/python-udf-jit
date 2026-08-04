"""Dedicated-worker binding for diagnostic compilation evidence.

This module is imported only after a frozen non-off diagnostic policy has
matched the current carrier.  Normal workers therefore never initialize a
bundle, provenance recorder, clock, or CinderX diagnostic observer.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import marshal
import threading
from dataclasses import dataclass
from types import FunctionType

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.identity import (
    capture_identities,
    code_identity_from_code,
)
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
    DiagnosticSourcePolicy,
    canonical_json_bytes,
)
from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile
from python_udf_jit.diagnostics.provenance import (
    UpperProvenanceRecorder,
    build_bytecode_artifacts,
    build_original_provenance,
    generated_ast_text,
    program_source_map_document,
)
from python_udf_jit.diagnostics.session import open_diagnostic_session
from python_udf_jit.protocol.artifact import PortableUdfArtifact


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redacted_attribute_value(value: str) -> str:
    """Replace canonical descriptor collections with fixed-size metadata."""

    try:
        descriptor = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(descriptor, list):
        kind = "sequence"
        shape = [len(descriptor)]
    elif isinstance(descriptor, dict):
        kind = "mapping"
        shape = [len(descriptor)]
    else:
        return value
    return json.dumps(
        {
            "count": len(descriptor),
            "kind": kind,
            "shape": shape,
            "sha256": _hash_text(value),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _redacted_attributes_document(
    attributes: list[list[str]],
) -> list[list[str]]:
    return [
        [name, _redacted_attribute_value(value)]
        for name, value in attributes
    ]


def _redacted_typed_module_document(module) -> dict[str, object]:
    document = module.to_document()
    for operation in document["operations"]:
        operation["attributes"] = _redacted_attributes_document(
            operation["attributes"]
        )
        literal = operation["literal"]
        if literal is None:
            continue
        operation["literal"] = {
            "kind": literal["kind"],
            "sha256": hashlib.sha256(
                literal["encoded_value"].encode("utf-8")
            ).hexdigest(),
        }
    return document


def _redacted_generated_artifacts(source: str) -> tuple[str, str]:
    module = ast.parse(source)

    class _RedactConstants(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.Name:
            try:
                encoded = marshal.dumps(node.value)
            except (TypeError, ValueError):
                encoded = type(node.value).__qualname__.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            replacement = ast.Name(
                id=f"_redacted_literal_{digest}",
                ctx=ast.Load(),
            )
            return ast.copy_location(replacement, node)

    redacted = ast.fix_missing_locations(_RedactConstants().visit(module))
    return ast.unparse(redacted) + "\n", generated_ast_text(module)


def _has_generic_cinderx_hir_summary(
    opcode_counts: tuple[tuple[str, int], ...],
) -> bool:
    """Recognize a typed region from ordinary CinderX HIR, not UDF kernels."""

    counts = dict(opcode_counts)
    if any(
        counts.get(opcode, 0)
        for opcode in (
            "UnicodeCountProperty",
            "UnicodeFsmSequence",
            "UnicodeMapSequence",
            "VectorCall",
        )
    ):
        return False
    if not counts.get("Phi", 0) or not counts.get("CondBranch", 0):
        return False
    return any(
        counts.get(opcode, 0)
        for opcode in (
            "IntBinaryOp",
            "PrimitiveSelect",
            "PrimitiveTableGet",
            "PrimitiveTableLookup",
            "SequenceBuilderAppend",
            "UnicodeClassify",
            "UnicodeRead",
        )
    )


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
        self._typed_regions_recording: set[str] = set()
        self._typed_regions_recorded: set[str] = set()
        self._analysis = None
        self._partial = False
        selector_kind, _, selector_value = policy.selector.partition(":")
        self._selected = policy.selector.startswith(
            ("artifact:", "candidate:")
        ) or (
            selector_kind == "udf"
            and code_identity_from_code(user_function.__code__).sha256.startswith(
                selector_value
            )
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

    def prepare_typed_compilation(
        self,
        function: FunctionType,
        generated_code_hash: str,
        operation_lines: tuple[tuple[str, int], ...],
    ) -> str:
        """Bind a typed generated function before CinderX compiles it."""

        if (
            not self._selected
            or self.policy.profile is not DiagnosticProfile.FULL
        ):
            return ""
        if (
            not isinstance(function, FunctionType)
            or len(generated_code_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in generated_code_hash
            )
        ):
            raise ValueError("typed_diagnostic_identity_invalid")
        compile_instance_id = f"typed-{generated_code_hash[:24]}"
        setattr(
            function,
            "__udfjit_generated_code_hash__",
            generated_code_hash,
        )
        if (
            not isinstance(operation_lines, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or type(item[1]) is not int
                or item[1] < 0
                for item in operation_lines
            )
        ):
            raise ValueError("typed_diagnostic_operation_lines_invalid")
        setattr(
            function,
            "__udfjit_typed_operation_lines__",
            operation_lines,
        )
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

    def record_typed_region_decision(self, request, decision) -> None:
        """Record the v2 typed path without coupling the normal Worker path.

        This method lives in the lazily imported full-diagnostics module.  The
        typed compiler only sees a structural sink protocol, so diagnostics=off
        neither imports this module nor builds any of these documents.
        """

        module = request.region
        selector_kind, _, selector_value = self._selector.partition(":")
        if selector_kind == "region":
            self._selected = module.semantic_hash.startswith(selector_value)
        if (
            not self._selected
            or self.policy.profile is not DiagnosticProfile.FULL
            or decision.status.value not in {"compiled", "unsupported"}
        ):
            return
        try:
            analysis = decision.worker_analysis
        except Exception:
            self.mark_partial()
            return
        if analysis is None:
            self.mark_partial()
            return
        with self._lock:
            if (
                module.semantic_hash in self._typed_regions_recorded
                or module.semantic_hash in self._typed_regions_recording
            ):
                return
            self._typed_regions_recording.add(module.semantic_hash)
        try:
            original_hash = code_identity_from_code(
                self._user_function.__code__
            ).sha256
            original = build_bytecode_artifacts(
                self._user_function.__code__,
                code_hash=original_hash,
            )
            original_provenance = build_original_provenance(
                self._user_function.__code__,
                code_hash=original_hash,
            )
            analysis_documents = analysis.to_documents()
            module_document = (
                module.to_document()
                if self.policy.source_policy is DiagnosticSourcePolicy.TEXT
                else _redacted_typed_module_document(module)
            )
            decision_document: dict[str, object] = {
                "driver_analysis_hint_matched": (
                    decision.driver_analysis_hint_matched
                ),
                "reason_code": decision.reason_code,
                "runtime": {
                    "call_count": request.runtime.call_count,
                    "deopt_count": request.runtime.deopt_count,
                },
                "schema_version": 1,
                "status": decision.status.value,
            }
            artifacts: list[tuple[str, str, object, str]] = [
                (
                    "typed/source-ranges.json",
                    "application/json",
                    program_source_map_document(original_provenance),
                    "source",
                ),
                (
                    "typed/bytecode-original.json",
                    "application/json",
                    original.json_document,
                    "bytecode",
                ),
                (
                    "typed/bytecode-original.dis",
                    "text/plain",
                    original.disassembly,
                    "bytecode",
                ),
                (
                    "typed/semantic-v2.json",
                    "application/json",
                    module_document,
                    "typed_semantic",
                ),
                (
                    "typed/semantic-v2.txt",
                    "text/plain",
                    json.dumps(
                        module_document,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    "typed_semantic",
                ),
                (
                    "typed/behavior-profile.json",
                    "application/json",
                    analysis_documents["behavior-profile"],
                    "behavior",
                ),
                (
                    "typed/type-evidence.json",
                    "application/json",
                    analysis_documents["type-evidence"],
                    "types",
                ),
                (
                    "typed/pattern-analysis.json",
                    "application/json",
                    analysis_documents["pattern-analysis"],
                    "patterns",
                ),
                (
                    "typed/decision.json",
                    "application/json",
                    decision_document,
                    "decision",
                ),
            ]
            chain = {
                "behavior": "available",
                "cinderx_hir": "unavailable",
                "cinderx_lir": "unavailable",
                "generated_bytecode": "unavailable",
                "generic_lowering": "unavailable",
                "machine": "unavailable",
                "original_bytecode": "available",
                "pattern_analysis": "available",
                "schema_version": 1,
                "source_ranges": "available",
                "typed_semantic": "available",
                "type_evidence": "available",
                "udf_physical_lowering": "not_applicable_backend_owned",
            }
            variant = decision.variant
            if variant is not None:
                lowering = variant.lowering
                backend = variant.backend
                generic_map = dict(lowering.operation_lines)
                generated = build_bytecode_artifacts(
                    variant.jit_function.__code__,
                    code_hash=variant.code_hash,
                )
                offsets_by_line: dict[int, list[int]] = {}
                for instruction in generated.json_document["instructions"]:
                    position = instruction["position"]
                    line = position["line"]
                    if type(line) is int:
                        offsets_by_line.setdefault(line, []).append(
                            instruction["offset"]
                        )
                original_offsets_by_line: dict[int, list[int]] = {}
                for instruction in original.json_document["instructions"]:
                    position = instruction["position"]
                    line = position["line"]
                    if type(line) is int:
                        original_offsets_by_line.setdefault(line, []).append(
                            instruction["offset"]
                        )
                provenance_entries: list[dict[str, object]] = []
                for operation in module.operations:
                    generic_line = generic_map[operation.operation_id]
                    provenance_entries.append(
                        {
                            "generated_bytecode_offsets": list(
                                offsets_by_line.get(generic_line, ())
                            ),
                            "generated_line": generic_line,
                            "generic_line": generic_line,
                            "hir_ids": [],
                            "lir_ids": [],
                            "machine_range_ids": [],
                            "operation_id": operation.operation_id,
                            "original_bytecode_offsets": list(
                                original_offsets_by_line.get(
                                    operation.source_offset,
                                    (),
                                )
                            ),
                            "source_offset": operation.source_offset,
                        }
                    )
                provenance = {
                    "entries": provenance_entries,
                    "generated_code_hash": variant.code_hash,
                    "module_hash": module.semantic_hash,
                    "schema_version": 1,
                }
                if self.policy.source_policy is DiagnosticSourcePolicy.TEXT:
                    generic_source = lowering.generated_source
                    generic_ast = lowering.generated_ast_text
                else:
                    generic_source, generic_ast = (
                        _redacted_generated_artifacts(
                            lowering.generated_source
                        )
                    )
                artifacts.extend(
                    (
                        (
                            "typed/specialization-plan.json",
                            "application/json",
                            lowering.plan.to_document(),
                            "lowering",
                        ),
                        (
                            "typed/generic-lowering.py",
                            "text/x-python",
                            generic_source,
                            "lowering",
                        ),
                        (
                            "typed/generic-lowering.ast",
                            "text/plain",
                            generic_ast,
                            "lowering",
                        ),
                        (
                            "typed/backend.json",
                            "application/json",
                            {
                                "execution_mode": backend.execution_mode,
                                "hir_opcode_counts": [
                                    list(value)
                                    for value in backend.hir_opcode_counts
                                ],
                                "jit_compiled": backend.jit_compiled,
                                "schema_version": 1,
                            },
                            "backend",
                        ),
                        (
                            "typed/generated-bytecode.json",
                            "application/json",
                            generated.json_document,
                            "bytecode",
                        ),
                        (
                            "typed/generated-bytecode.dis",
                            "text/plain",
                            generated.disassembly,
                            "bytecode",
                        ),
                    )
                )
                chain["generic_lowering"] = "available"
                chain["generated_bytecode"] = "available"
                if _has_generic_cinderx_hir_summary(
                    backend.hir_opcode_counts
                ):
                    chain["cinderx_hir"] = "available_summary"
                compile_instance_id = (
                    f"typed-{variant.code_hash[:24]}"
                    if getattr(
                        variant.jit_function,
                        "__udfjit_generated_code_hash__",
                        None,
                    )
                    == variant.code_hash
                    else ""
                )
                if compile_instance_id:
                    from cinderx import jit as jit_module

                    diagnostics = collect_cinderx_compilation_diagnostics(
                        jit_module,
                        variant.jit_function,
                        compile_instance_id=compile_instance_id,
                        generated_code_hash=variant.code_hash,
                    )
                    cinderx = build_cinderx_artifacts(diagnostics)
                    artifacts.extend(
                        (
                            (
                                "typed/cinderx/hir.final.json",
                                "application/json",
                                cinderx.hir_json,
                                "hir",
                            ),
                            (
                                "typed/cinderx/hir.final.txt",
                                "text/plain",
                                cinderx.hir_text,
                                "hir",
                            ),
                            (
                                "typed/cinderx/lir-origin.json",
                                "application/json",
                                cinderx.lir_json,
                                "lir",
                            ),
                            (
                                "typed/cinderx/lir.txt",
                                "text/plain",
                                cinderx.lir_text,
                                "lir",
                            ),
                            (
                                "typed/cinderx/machine-ranges.json",
                                "application/json",
                                cinderx.machine_ranges_json,
                                "machine",
                            ),
                            (
                                "typed/cinderx/machine-ranges.txt",
                                "text/plain",
                                cinderx.machine_ranges_text,
                                "machine",
                            ),
                            (
                                "typed/cinderx/compile-stats.json",
                                "application/json",
                                cinderx.compile_stats_json,
                                "cinderx",
                            ),
                        )
                    )
                    if diagnostics.status.value == "available":
                        hir_by_offset: dict[int, list[str]] = {}
                        for node in diagnostics.hir_nodes:
                            if node.bytecode_offset is not None:
                                hir_by_offset.setdefault(
                                    node.bytecode_offset,
                                    [],
                                ).append(node.hir_id)
                        lir_by_hir: dict[str, set[str]] = {}
                        for node in diagnostics.lir_nodes:
                            for hir_id in node.hir_ids:
                                lir_by_hir.setdefault(hir_id, set()).add(
                                    node.lir_id
                                )
                        ranges_by_hir: dict[str, set[str]] = {}
                        ranges_by_lir: dict[str, set[str]] = {}
                        for item in diagnostics.machine_ranges:
                            for hir_id in item.hir_ids:
                                ranges_by_hir.setdefault(hir_id, set()).add(
                                    item.range_id
                                )
                            for lir_id in item.lir_ids:
                                ranges_by_lir.setdefault(lir_id, set()).add(
                                    item.range_id
                                )
                        for entry in provenance_entries:
                            offsets = entry["generated_bytecode_offsets"]
                            hir_ids = sorted(
                                {
                                    hir_id
                                    for offset in offsets
                                    for hir_id in hir_by_offset.get(offset, ())
                                },
                                key=int,
                            )
                            lir_ids = sorted(
                                {
                                    lir_id
                                    for hir_id in hir_ids
                                    for lir_id in lir_by_hir.get(hir_id, ())
                                },
                                key=int,
                            )
                            range_ids = sorted(
                                {
                                    range_id
                                    for hir_id in hir_ids
                                    for range_id in ranges_by_hir.get(hir_id, ())
                                }
                                | {
                                    range_id
                                    for lir_id in lir_ids
                                    for range_id in ranges_by_lir.get(lir_id, ())
                                },
                                key=int,
                            )
                            entry["hir_ids"] = hir_ids
                            entry["lir_ids"] = lir_ids
                            entry["machine_range_ids"] = range_ids
                        chain["cinderx_hir"] = "available"
                        chain["cinderx_lir"] = "available"
                        chain["machine"] = "available"
                artifacts.append(
                    (
                        "typed/operation-provenance.json",
                        "application/json",
                        provenance,
                        "provenance",
                    )
                )
            artifacts.append(
                (
                    "typed/chain-status.json",
                    "application/json",
                    chain,
                    "reports",
                )
            )
            emitted = True
            for path, media_type, payload, layer in artifacts:
                emitted = (
                    self._artifact(
                        path,
                        media_type,
                        payload,
                        layer=layer,
                    )
                    and emitted
                )
            if emitted:
                with self._lock:
                    self._typed_regions_recorded.add(module.semantic_hash)
        except Exception:
            self.mark_partial()
        finally:
            with self._lock:
                self._typed_regions_recording.discard(module.semantic_hash)

    def record_typed_runtime_summary(
        self,
        document: dict[str, object],
    ) -> bool:
        """Attach bounded Worker lifecycle counters to a typed diagnostic bundle."""

        if (
            not self._selected
            or self.policy.profile is not DiagnosticProfile.FULL
        ):
            return False
        allowed = {
            "calls",
            "compile_attempts",
            "compile_successes",
            "execution_mode",
            "fallbacks",
            "guard_misses",
            "hits",
            "reason_code",
            "schema_version",
            "semantic_hash",
            "wrapper_depth",
        }
        if set(document) != allowed:
            self.mark_partial()
            return False
        return self._artifact(
            "typed/runtime-summary.json",
            "application/json",
            document,
            layer="runtime",
        )

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
            artifacts = [
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
            ]
            if self.policy.source_policy is DiagnosticSourcePolicy.TEXT:
                try:
                    source_text = inspect.getsource(self._user_function)
                except (OSError, TypeError):
                    self.mark_partial()
                    artifacts.insert(
                        2,
                        (
                            "source/text-status.json",
                            "application/json",
                            {
                                "schema_version": 1,
                                "status": "unavailable",
                                "unavailable_reason": (
                                    "source_text_unavailable"
                                ),
                            },
                            "source",
                        ),
                    )
                else:
                    artifacts.insert(
                        2,
                        (
                            "source/source.py",
                            "text/x-python",
                            source_text,
                            "source",
                        ),
                    )
            for path, media_type, payload, layer in artifacts:
                if not self._artifact(
                    path,
                    media_type,
                    payload,
                    layer=layer,
                ):
                    self.mark_partial()
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
