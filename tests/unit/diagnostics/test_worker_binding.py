from __future__ import annotations

import dis
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from python_udf_jit.compiler.typed_frontend import capture_typed_loop
from python_udf_jit.compiler.identity import code_identity_from_code
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
from python_udf_jit.diagnostics.bundle import (
    read_artifact_bytes,
    read_bundle,
    read_json_artifact,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    OFF_DIAGNOSTIC_POLICY,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile
from python_udf_jit.diagnostics.report import validate_diagnostic_bundle
from python_udf_jit.diagnostics.worker_runtime import (
    WorkerDiagnosticRuntime,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.worker import (
    WorkerDiagnosticPerfEvidence,
    WorkerRuntimeContext,
    WorkerScalarAdapter,
)
from python_udf_jit.runtime.variant import WorkerProcessKey
from python_udf_jit.provider.scalar_python.compiler import (
    ScalarLoweringSnapshot,
)
from python_udf_jit.provider.scalar_python.typed_loop import (
    BackendCompilation,
    CompileStatus,
    RuntimeFeedback,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
    lower_unicode_count_physical,
    lower_unicode_fsm_physical,
    lower_unicode_map_physical,
)
from tests.unit.diagnostics.test_cinderx_bridge import _document
from tests.unit.diagnostics.test_provenance import (
    _module_and_graph,
    _source_map,
)


def _identity(value):
    return value


def _generated(value):
    return value


def _typed_alpha_count(text: str) -> int:
    return sum(1 for character in text if character.isalpha())


def _typed_alpha_ratio(
    text: str,
    threshold: float = 0.73123456789,
) -> bool:
    return sum(1 for character in text if character.isalpha()) / len(text) >= threshold


def _typed_symbol_remap(text: str) -> str:
    substitutions = str.maketrans({"α": "a", "β": "b", "→": ">"})
    return text.translate(substitutions)


_TYPED_SPACE_RUN = re.compile(r"\s+")


def _typed_space_collapse(text: str) -> str:
    return _TYPED_SPACE_RUN.sub(" ", text).strip()


class _TypedDiagnosticBackend:
    adapter_version = "typed-diagnostic-test-v1"

    def compile(self, lowering):
        return self._compile(lowering, None)

    def compile_with_diagnostics(self, lowering, diagnostic_sink):
        return self._compile(lowering, diagnostic_sink)

    def _compile(self, lowering, diagnostic_sink):
        methods = (
            "isalnum",
            "isalpha",
            "isdecimal",
            "isdigit",
            "isnumeric",
            "isspace",
        )

        def helper(text, property_id):
            return sum(
                getattr(character, methods[property_id])()
                for character in text
            )

        physical = lower_unicode_count_physical(lowering, helper)
        if diagnostic_sink is not None:
            diagnostic_sink.prepare_typed_compilation(
                physical.function,
                physical.generated_code_hash,
            )
        return BackendCompilation(
            True,
            "test_unicode_property_hir",
            (("UnicodeCountProperty", 1),),
            physical,
        )


class _GenericTypedDiagnosticBackend:
    adapter_version = "generic-typed-diagnostic-test-v1"

    def compile(self, _lowering):
        return BackendCompilation(True, "test_generic_bytecode")


class _SequenceTypedDiagnosticBackend:
    adapter_version = "sequence-typed-diagnostic-test-v1"

    def compile(self, lowering):
        operation_names = {
            operation.op for operation in lowering.module.operations
        }
        if "immutable.lookup" in operation_names:
            physical = lower_unicode_map_physical(
                lowering,
                lambda text, _keys, _values: text,
            )
            opcode = "UnicodeMapSequence"
        elif "fsm.transition" in operation_names:
            physical = lower_unicode_fsm_physical(
                lowering,
                lambda text, _property, _state, _descriptor: text,
            )
            opcode = "UnicodeFsmSequence"
        else:
            raise AssertionError("sequence descriptor expected")
        return BackendCompilation(
            True,
            "test_unicode_sequence_hir",
            ((opcode, 1),),
            physical,
        )


class WorkerDiagnosticBindingTests(unittest.TestCase):
    def _context(self, diagnostic_policy):
        return WorkerRuntimeContext(
            "run-a",
            WorkerProcessKey(
                "epoch-a",
                "node-a",
                "worker-a",
                os.getpid(),
                "generation-a",
            ),
            "partition-a",
            "attempt-a",
            diagnostic_policy=diagnostic_policy,
            diagnostic_bootstrapped=(
                diagnostic_policy is not OFF_DIAGNOSTIC_POLICY
            ),
        )

    def _full_perf_policy(self, root: Path):
        return resolve_diagnostic_policy(
            {
                "UDFJIT_DIAGNOSTICS": "full",
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "record",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            },
            DiagnosticRuntimeContext(
                dedicated_worker=True,
                workspace_root=root / "workspace",
                home_root=root / "home",
            ),
        )

    @staticmethod
    def _perf_profile() -> NormalizedPerfProfile:
        process_id = os.getpid()
        return NormalizedPerfProfile.from_document(
            {
                "schema_version": 1,
                "run_id": "run-a",
                "process_id": process_id,
                "event": "cycles",
                "lost_samples": 0,
                "samples": [
                    {
                        "sample_id": "sample-1",
                        "pid": process_id,
                        "tid": process_id,
                        "timestamp_ns": 1,
                        "event": "cycles",
                        "ip": 4096,
                        "period": 7,
                        "runtime_phase": "execute",
                        "symbol_sha256": "a" * 64,
                    }
                ],
            }
        )

    def test_full_worker_requires_pre_cinderx_bootstrap_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_environment = {
                "UDFJIT_CLUSTER_EPOCH": "epoch-a",
                "UDFJIT_RUN_ID": "run-a",
                "UDFJIT_NODE_ID": "node-a",
                "UDFJIT_ACTOR_WORKER_ID": "worker-a",
                "UDFJIT_PARTITION_ID": "partition-a",
            }
            diagnostics_environment = {
                "UDFJIT_DIAGNOSTICS": "full",
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            }
            bootstrap = DiagnosticRuntimeContext(
                dedicated_worker=True,
                workspace_root=root / "workspace",
                home_root=root / "home",
            )
            with mock.patch.dict(
                os.environ,
                identity_environment,
                clear=True,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "diagnostic_backend_bootstrap_missing",
                ):
                    WorkerRuntimeContext.from_environment(
                        diagnostic_environment=diagnostics_environment,
                        diagnostic_runtime=bootstrap,
                    )
                context = WorkerRuntimeContext.from_environment(
                    diagnostic_environment={
                        **diagnostics_environment,
                        "PYTHONJITUDFDIAGNOSTICS": "1",
                    },
                    diagnostic_runtime=bootstrap,
                )

        self.assertTrue(context.diagnostic_bootstrapped)

    def test_off_constructs_the_normal_factory_without_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
            ).finalize(b"opaque")
            perf_provider = mock.Mock(
                side_effect=AssertionError("off path must stay inert")
            )
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=self._context(OFF_DIAGNOSTIC_POLICY),
                diagnostic_perf_provider=perf_provider,
            )
            self.assertIsNone(
                adapter._provider_factory._diagnostic_observer
            )
            adapter.close()
            perf_provider.assert_not_called()
            self.assertFalse((root / "diagnostics").exists())

    def test_full_binds_an_observer_and_finalizes_a_private_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"opaque"
            artifact_hash = hashlib.sha256(payload).hexdigest()
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": (
                        f"artifact:{artifact_hash}"
                    ),
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
                diagnostic_policy=policy,
            ).finalize(payload)
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=self._context(policy),
            )
            observer = adapter._provider_factory._diagnostic_observer
            self.assertIsNotNone(observer)
            adapter.close()
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            self.assertTrue((bundles[0] / "COMPLETE").is_file())

    def test_full_observer_writes_the_readable_compilation_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                    "UDFJIT_DIAGNOSTIC_SOURCE": "text",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(4 * 1024 * 1024),
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            runtime = WorkerDiagnosticRuntime(
                policy,
                run_id="run-a",
                runtime_mode="auto",
                process_key="worker-a",
                process_id=os.getpid(),
                user_function=_identity,
            )
            module, graph = _module_and_graph()
            artifact = SimpleNamespace(
                fallback_identity=SimpleNamespace(code_sha256="a" * 64),
                semantic_core_module=module,
                semantic_region_graph=graph,
            )
            key = SimpleNamespace(sha256="c" * 64)
            identities = SimpleNamespace(
                code=SimpleNamespace(sha256="a" * 64),
                source=SimpleNamespace(code_sha256=module.function_id),
            )
            program = SimpleNamespace(
                frontend=SimpleNamespace(source_map=_source_map())
            )
            with (
                mock.patch(
                    "python_udf_jit.diagnostics.worker_runtime."
                    "capture_identities",
                    return_value=identities,
                ),
                mock.patch(
                    "python_udf_jit.diagnostics.worker_runtime."
                    "analyze_function",
                    return_value=program,
                ),
            ):
                sink = runtime.provenance_sink(artifact, key)
            self.assertIsNotNone(sink)
            generated_hash = "b" * 64
            sink.record_scalar_lowering(
                ScalarLoweringSnapshot(
                    module.semantic_hash,
                    graph.semantic_hash,
                    generated_hash,
                    _generated.__code__,
                    "Module(body=[])",
                    (),
                )
            )
            compiled = SimpleNamespace(
                code_hash=generated_hash,
                semantic_hash=module.semantic_hash,
                jit_function=_generated,
            )
            compile_instance_id = runtime.prepare_compilation(compiled, key)
            document = _document()
            document["compile_instance_id"] = compile_instance_id
            document["generated_code_hash"] = generated_hash

            class StructuredJit:
                def get_udfjit_compilation_diagnostics(
                    self,
                    _function,
                    _compile_instance_id,
                ):
                    return document

            runtime.record_compilation(
                StructuredJit(),
                compiled,
                key,
                compile_instance_id,
            )
            bundle_ref = runtime.finalize()
            self.assertIsNotNone(bundle_ref)
            bundle = read_bundle(bundle_ref.path)
            paths = {artifact.path for artifact in bundle.artifacts}
            self.assertTrue(
                {
                    "source/ranges.json",
                    "source/source.py",
                    "bytecode/original.dis",
                    "semantic/core.final.txt",
                    "lowering/generated_ast.txt",
                    "bytecode/generated.dis",
                    "cinderx/hir.final.txt",
                    "cinderx/lir.txt",
                    "cinderx/machine-ranges.txt",
                    "provenance/map.json",
                }.issubset(paths)
            )
            provenance = read_json_artifact(
                bundle,
                "provenance/map.json",
            )

        self.assertTrue(
            any(
                node["layer"] == "machine"
                for node in provenance["nodes"]
            )
        )
        self.assertEqual(
            _generated.__udfjit_generated_code_hash__,
            generated_hash,
        )

    def test_full_typed_path_records_each_generic_and_physical_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(4 * 1024 * 1024),
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            runtime = WorkerDiagnosticRuntime(
                policy,
                run_id="run-a",
                runtime_mode="auto",
                process_key="worker-a",
                process_id=os.getpid(),
                user_function=_typed_alpha_ratio,
            )
            captured = capture_typed_loop(
                _typed_alpha_ratio,
                input_types=(EXACT_UNICODE,),
            )
            backend = _TypedDiagnosticBackend()
            compiler = TypedRegionCompiler(
                backend,
                call_threshold=1,
                negative_ttl_ns=1_000_000_000,
                diagnostic_sink=runtime,
            )
            deferred = compiler.compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(call_count=0, deopt_count=0),
                    captured.analysis.to_documents(),
                    captured.runtime_guard,
                )
            )
            self.assertEqual(deferred.status, CompileStatus.DEFERRED)

            original_compile = backend.compile_with_diagnostics
            fail_once = mock.Mock(
                side_effect=RuntimeError("synthetic first backend failure")
            )
            backend.compile_with_diagnostics = fail_once
            failed = compiler.compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(call_count=1, deopt_count=0),
                    captured.analysis.to_documents(),
                    captured.runtime_guard,
                )
            )
            self.assertEqual(failed.status, CompileStatus.FAILURE)
            backend.compile_with_diagnostics = original_compile
            compiler._negative.clear(compiler._cache_key(captured.module))

            def structured_document(function, compile_instance_id):
                offsets = [
                    instruction.offset
                    for instruction in dis.get_instructions(function)
                ]
                return {
                    "schema_version": 1,
                    "status": "available",
                    "compile_instance_id": compile_instance_id,
                    "generated_code_hash": getattr(
                        function,
                        "__udfjit_generated_code_hash__",
                    ),
                    "jit_compiled": True,
                    "unavailable_reason": None,
                    "jit_gate_reason": None,
                    "code_start": 4096,
                    "code_size": len(offsets) * 8,
                    "stack_size": 32,
                    "spill_stack_size": 8,
                    "pass_timings": [],
                    "hir_nodes": [
                        {
                            "hir_id": str(index),
                            "opcode": "BytecodeOrigin",
                            "bytecode_offset": offset,
                            "synthetic_kind": None,
                        }
                        for index, offset in enumerate(offsets)
                    ],
                    "lir_nodes": [
                        {
                            "lir_id": str(index),
                            "opcode": "LoweredOrigin",
                            "hir_ids": [str(index)],
                            "synthetic_kind": None,
                        }
                        for index, _offset in enumerate(offsets)
                    ],
                    "machine_ranges": [
                        {
                            "range_id": str(index),
                            "start": 4096 + index * 8,
                            "end": 4096 + (index + 1) * 8,
                            "section": "hot",
                            "symbol": "generated_typed_region",
                            "lir_ids": [str(index)],
                            "hir_ids": [str(index)],
                            "synthetic_kind": None,
                        }
                        for index, _offset in enumerate(offsets)
                    ],
                    "deopt_metadata": [],
                }

            jit_module = ModuleType("cinderx.jit")
            jit_module.get_udfjit_compilation_diagnostics = (
                structured_document
            )
            cinderx_module = ModuleType("cinderx")
            cinderx_module.jit = jit_module
            with mock.patch.dict(
                sys.modules,
                {
                    "cinderx": cinderx_module,
                    "cinderx.jit": jit_module,
                },
            ):
                decision = compiler.compile(
                    TypedRegionCompileRequest(
                        captured.module,
                        RuntimeFeedback(call_count=1, deopt_count=0),
                        captured.analysis.to_documents(),
                        captured.runtime_guard,
                    )
                )
                repeated = compiler.compile(
                    TypedRegionCompileRequest(
                        captured.module,
                        RuntimeFeedback(call_count=1, deopt_count=0),
                        captured.analysis.to_documents(),
                        captured.runtime_guard,
                    )
                )
            self.assertEqual(decision.status, CompileStatus.COMPILED)
            self.assertEqual(repeated.status, CompileStatus.COMPILED)
            self.assertEqual(
                decision.variant("A-中"),
                _typed_alpha_ratio("A-中"),
            )
            self.assertTrue(
                runtime.record_typed_runtime_summary(
                    {
                        "calls": 128,
                        "compile_attempts": 1,
                        "compile_successes": 1,
                        "execution_mode": "test_typed_diagnostic",
                        "fallbacks": 1,
                        "guard_misses": 0,
                        "hits": 127,
                        "reason_code": "typed_loop_hit",
                        "schema_version": 1,
                        "semantic_hash": captured.module.semantic_hash,
                        "wrapper_depth": 2,
                    }
                )
            )
            bundle_ref = runtime.finalize()
            self.assertIsNotNone(bundle_ref)
            bundle = read_bundle(bundle_ref.path)
            paths = {artifact.path for artifact in bundle.artifacts}
            chain = read_json_artifact(bundle, "typed/chain-status.json")
            provenance = read_json_artifact(
                bundle,
                "typed/operation-provenance.json",
            )
            canary = b"0.73123456789"
            for artifact in bundle.artifacts:
                self.assertNotIn(
                    canary,
                    read_artifact_bytes(bundle, artifact.path),
                    artifact.path,
                )

        self.assertTrue(
            {
                "typed/source-ranges.json",
                "typed/bytecode-original.dis",
                "typed/semantic-v2.txt",
                "typed/behavior-profile.json",
                "typed/type-evidence.json",
                "typed/pattern-analysis.json",
                "typed/specialization-plan.json",
                "typed/generic-lowering.py",
                "typed/physical-lowering.py",
                "typed/generated-bytecode.dis",
                "typed/backend.json",
                "typed/cinderx/hir.final.txt",
                "typed/cinderx/lir.txt",
                "typed/cinderx/machine-ranges.txt",
                "typed/operation-provenance.json",
                "typed/runtime-summary.json",
                "typed/chain-status.json",
            }.issubset(paths)
        )
        self.assertEqual(chain["cinderx_hir"], "available")
        self.assertEqual(chain["cinderx_lir"], "available")
        self.assertEqual(chain["machine"], "available")
        self.assertEqual(chain["physical_lowering"], "available")
        self.assertTrue(
            any(entry["hir_ids"] for entry in provenance["entries"])
        )
        self.assertTrue(
            all(
                entry["original_bytecode_offsets"]
                for entry in provenance["entries"]
            )
        )
        self.assertTrue(
            any(entry["machine_range_ids"] for entry in provenance["entries"])
        )

    def test_sequence_descriptors_follow_the_diagnostic_source_policy(
        self,
    ) -> None:
        cases = (
            (
                _typed_symbol_remap,
                ("[945,946,8594]", "[97,98,62]"),
            ),
            (
                _typed_space_collapse,
                (
                    "[1,0,1,2,1,2]",
                    "[1,0,1,0,3,0]",
                    "[0,0,0,0,32,0]",
                ),
            ),
        )
        for source_policy in ("ranges", "text"):
            for function, canaries in cases:
                with self.subTest(
                    source_policy=source_policy,
                    function=function.__name__,
                ), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy = resolve_diagnostic_policy(
                        {
                            "UDFJIT_DIAGNOSTICS": "full",
                            "UDFJIT_DIAGNOSTIC_DIR": str(
                                root / "diagnostics"
                            ),
                            "UDFJIT_DIAGNOSTIC_FILTER": (
                                "candidate:candidate-a"
                            ),
                            "UDFJIT_DIAGNOSTIC_SOURCE": source_policy,
                            "UDFJIT_DIAGNOSTIC_PERF": "off",
                            "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                            "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(
                                4 * 1024 * 1024
                            ),
                        },
                        DiagnosticRuntimeContext(
                            dedicated_worker=True,
                            workspace_root=root / "workspace",
                            home_root=root / "home",
                        ),
                    )
                    runtime = WorkerDiagnosticRuntime(
                        policy,
                        run_id="run-a",
                        runtime_mode="auto",
                        process_key="worker-a",
                        process_id=os.getpid(),
                        user_function=function,
                    )
                    captured = capture_typed_loop(
                        function,
                        input_types=(EXACT_UNICODE,),
                    )
                    decision = TypedRegionCompiler(
                        _SequenceTypedDiagnosticBackend(),
                        call_threshold=1,
                        negative_ttl_ns=1_000_000_000,
                        diagnostic_sink=runtime,
                    ).compile(
                        TypedRegionCompileRequest(
                            captured.module,
                            RuntimeFeedback(call_count=1, deopt_count=0),
                            captured.analysis.to_documents(),
                            captured.runtime_guard,
                        )
                    )
                    self.assertEqual(decision.status, CompileStatus.COMPILED)
                    bundle_ref = runtime.finalize()
                    self.assertIsNotNone(bundle_ref)
                    bundle = read_bundle(bundle_ref.path)
                    semantic = read_json_artifact(
                        bundle,
                        "typed/semantic-v2.json",
                    )
                    physical = read_json_artifact(
                        bundle,
                        "typed/physical-lowering.json",
                    )
                    semantic_text = json.dumps(
                        semantic,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    physical_text = json.dumps(
                        physical,
                        ensure_ascii=False,
                        sort_keys=True,
                    )

                    if source_policy == "text":
                        for canary in canaries:
                            self.assertIn(canary, semantic_text)
                            self.assertIn(canary, physical_text)
                        continue

                    for canary in canaries:
                        self.assertNotIn(canary, semantic_text)
                        self.assertNotIn(canary, physical_text)
                    semantic_metadata = [
                        json.loads(value)
                        for operation in semantic["operations"]
                        for _name, value in operation["attributes"]
                        if value.startswith('{"count":')
                    ]
                    physical_metadata = [
                        json.loads(value)
                        for _name, value in physical["physical_attributes"]
                        if value.startswith('{"count":')
                    ]
                    expected_counts = sorted(
                        len(json.loads(canary)) for canary in canaries
                    )
                    for metadata in (
                        semantic_metadata,
                        physical_metadata,
                    ):
                        self.assertEqual(
                            sorted(value["count"] for value in metadata),
                            expected_counts,
                        )
                        self.assertTrue(
                            all(
                                value["shape"] == [value["count"]]
                                and len(value["sha256"]) == 64
                                for value in metadata
                            )
                        )

    def test_udf_selector_enables_typed_worker_recording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code_sha256 = code_identity_from_code(
                _typed_alpha_ratio.__code__
            ).sha256
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": f"udf:{code_sha256[:16]}",
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(4 * 1024 * 1024),
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            runtime = WorkerDiagnosticRuntime(
                policy,
                run_id="run-a",
                runtime_mode="auto",
                process_key="worker-a",
                process_id=os.getpid(),
                user_function=_typed_alpha_ratio,
            )
            captured = capture_typed_loop(
                _typed_alpha_ratio,
                input_types=(EXACT_UNICODE,),
            )
            decision = TypedRegionCompiler(
                _GenericTypedDiagnosticBackend(),
                call_threshold=1,
                negative_ttl_ns=1_000_000_000,
                diagnostic_sink=runtime,
            ).compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(call_count=1, deopt_count=0),
                    captured.analysis.to_documents(),
                    captured.runtime_guard,
                )
            )
            bundle_ref = runtime.finalize()
            self.assertEqual(decision.status, CompileStatus.COMPILED)
            self.assertIsNotNone(bundle_ref)
            bundle = read_bundle(bundle_ref.path)
            paths = {artifact.path for artifact in bundle.artifacts}

        self.assertIn(
            "typed/semantic-v2.json",
            paths,
        )

    def test_full_typed_path_retries_after_unrecordable_guard_decision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(4 * 1024 * 1024),
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            runtime = WorkerDiagnosticRuntime(
                policy,
                run_id="run-a",
                runtime_mode="auto",
                process_key="worker-a",
                process_id=os.getpid(),
                user_function=_typed_alpha_ratio,
            )
            captured = capture_typed_loop(
                _typed_alpha_ratio,
                input_types=(EXACT_UNICODE,),
            )
            compiler = TypedRegionCompiler(
                _GenericTypedDiagnosticBackend(),
                call_threshold=1,
                negative_ttl_ns=1_000_000_000,
                diagnostic_sink=runtime,
            )
            missing_guard = compiler.compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(call_count=1, deopt_count=0),
                )
            )

            self.assertEqual(missing_guard.status, CompileStatus.UNSUPPORTED)
            self.assertEqual(
                missing_guard.reason_code,
                "runtime_dependency_guard_missing",
            )
            self.assertNotIn(
                captured.module.semantic_hash,
                runtime._typed_regions_recorded,
            )

            compiled = compiler.compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(call_count=1, deopt_count=0),
                    captured.analysis.to_documents(),
                    captured.runtime_guard,
                )
            )

            self.assertEqual(compiled.status, CompileStatus.COMPILED)
            self.assertEqual(
                compiled.variant("A-中"),
                _typed_alpha_ratio("A-中"),
            )
            bundle_ref = runtime.finalize()
            self.assertIsNotNone(bundle_ref)
            bundle = read_bundle(bundle_ref.path)
            paths = {artifact.path for artifact in bundle.artifacts}
            decision = read_json_artifact(bundle, "typed/decision.json")
            chain = read_json_artifact(bundle, "typed/chain-status.json")

        self.assertTrue(
            {
                "typed/semantic-v2.json",
                "typed/behavior-profile.json",
                "typed/type-evidence.json",
                "typed/pattern-analysis.json",
                "typed/generated-bytecode.json",
                "typed/operation-provenance.json",
                "typed/chain-status.json",
            }.issubset(paths)
        )
        self.assertEqual(decision["status"], "compiled")
        self.assertEqual(chain["generic_lowering"], "available")
        self.assertEqual(chain["generated_bytecode"], "available")

    def test_full_perf_evidence_is_ingested_before_worker_finalization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_perf_policy(root)
            context = self._context(policy)
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
                diagnostic_policy=policy,
            ).finalize(b"opaque")
            provider_calls = []

            def provide_perf_evidence():
                provider_calls.append("called")
                return WorkerDiagnosticPerfEvidence(
                    context.process,
                    self._perf_profile(),
                    b"raw-perf-data",
                )

            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=context,
                diagnostic_perf_provider=provide_perf_evidence,
            )
            adapter.close()

            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(provider_calls, ["called"])
            self.assertEqual(len(bundles), 1)
            validation = validate_diagnostic_bundle(bundles[0])
            bundle = read_bundle(bundles[0])
            paths = {artifact.path for artifact in bundle.artifacts}
            self.assertEqual(validation["bundle_status"], "complete")
            self.assertIn("perf/samples.json", paths)
            self.assertIn("perf/perf.data", paths)
            profile = read_json_artifact(bundle, "perf/samples.json")
            self.assertEqual(profile["run_id"], "run-a")
            self.assertEqual(profile["process_id"], os.getpid())

    def test_full_perf_without_evidence_finalizes_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_perf_policy(root)
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
                diagnostic_policy=policy,
            ).finalize(b"opaque")
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=self._context(policy),
            )

            adapter.close()

            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            self.assertEqual(
                validate_diagnostic_bundle(bundles[0])["bundle_status"],
                "partial",
            )

    def test_full_perf_rejects_another_process_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_perf_policy(root)
            context = self._context(policy)
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
                diagnostic_policy=policy,
            ).finalize(b"opaque")
            stale_process = WorkerProcessKey(
                context.process.cluster_epoch,
                context.process.node_id,
                context.process.actor_worker_id,
                context.process.pid,
                "stale-generation",
            )
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=context,
            )

            accepted = adapter.record_diagnostic_perf_evidence(
                WorkerDiagnosticPerfEvidence(
                    stale_process,
                    self._perf_profile(),
                )
            )
            adapter.close()

            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            bundle = read_bundle(bundles[0])
            self.assertFalse(accepted)
            self.assertEqual(bundle.status.value, "partial")
            self.assertNotIn(
                "perf/samples.json",
                {artifact.path for artifact in bundle.artifacts},
            )

    def test_explicit_perf_ingestion_prevents_duplicate_provider_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_perf_policy(root)
            context = self._context(policy)
            carrier = ProductionCarrierState.placeholder(
                "candidate-a",
                "a" * 64,
                diagnostic_policy=policy,
            ).finalize(b"opaque")
            provider = mock.Mock(
                side_effect=AssertionError("perf evidence already recorded")
            )
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=context,
                diagnostic_perf_provider=provider,
            )

            accepted = adapter.record_diagnostic_perf_evidence(
                WorkerDiagnosticPerfEvidence(
                    context.process,
                    self._perf_profile(),
                )
            )
            adapter.close()

            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            provider.assert_not_called()
            self.assertTrue(accepted)
            self.assertEqual(
                validate_diagnostic_bundle(bundles[0])["bundle_status"],
                "complete",
            )


if __name__ == "__main__":
    unittest.main()
