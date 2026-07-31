from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.diagnostics.bundle import read_bundle, read_json_artifact
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    OFF_DIAGNOSTIC_POLICY,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.worker_runtime import (
    WorkerDiagnosticRuntime,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.worker import (
    WorkerRuntimeContext,
    WorkerScalarAdapter,
)
from python_udf_jit.runtime.variant import WorkerProcessKey
from python_udf_jit.provider.scalar_python.compiler import (
    ScalarLoweringSnapshot,
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
            adapter = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=_identity,
                carrier=carrier,
                logical_schema="schema",
                context=self._context(OFF_DIAGNOSTIC_POLICY),
            )
            self.assertIsNone(
                adapter._provider_factory._diagnostic_observer
            )
            adapter.close()
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


if __name__ == "__main__":
    unittest.main()
