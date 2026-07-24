from __future__ import annotations

import math
import os
import json
import hashlib
import unittest
from pathlib import Path


def _affine(value: float) -> float:
    return value * 2.0 + 3.0


def _changed_region(value: float) -> float:
    return value * 2.0 - 4.0


class _WorkerScalarSlotProbe:
    def run(self, cluster_epoch: str) -> dict[str, object]:
        import cinderx.jit
        import ray
        from cinderjit import _udf_data_load_f64, _udf_guard_data_handle

        from python_udf_jit.compiler.capture import CaptureRequest, capture
        from python_udf_jit.compiler.core_ir import lower_capture
        from python_udf_jit.compiler.region import form_verified_region
        from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
        from python_udf_jit.provider.scalar_python.compiler import compile_scalar_region
        from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
        from python_udf_jit.runtime.layout import CinderXScalarSlotBackend

        runtime_context = ray.get_runtime_context()
        registry = CapabilityRegistry(epoch=cluster_epoch)
        executor = ScalarExecutor(registry)
        compiled_regions = []
        results = []
        opcode_counts = []

        for function in (_affine, _changed_region):
            module = lower_capture(capture(CaptureRequest(function)))
            region = form_verified_region(module)
            compiled = compile_scalar_region(
                module,
                region,
                guard_function=_udf_guard_data_handle,
                load_function=_udf_data_load_f64,
                execution_mode="cinderx-jit",
                argument_kind="backend",
            )
            if not cinderx.jit.force_compile(compiled._function):
                raise AssertionError("CinderX rejected the verified scalar Region")
            if not cinderx.jit.is_jit_compiled(compiled._function):
                raise AssertionError("verified scalar Region is not JIT compiled")
            counts = cinderx.jit.get_function_hir_opcode_counts(compiled._function)
            if counts.get("LoadUdfDataF64", 0) != 1:
                raise AssertionError(f"unexpected UDF data-load HIR counts: {counts}")

            backend = CinderXScalarSlotBackend()
            handle = registry.register(backend)
            try:
                value = 1.25
                actual = executor.execute(compiled, handle, value)
                expected = function(value)
                if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.0):
                    raise AssertionError((actual, expected))
                descriptor = registry.descriptor(handle)
                if descriptor.process != registry.process_identity:
                    raise AssertionError("descriptor escaped its process generation")
                if descriptor.epoch != cluster_epoch:
                    raise AssertionError("descriptor epoch drift")
                compiled_regions.append(
                    {
                        "semantic_hash": compiled.semantic_hash,
                        "code_hash": compiled.code_hash,
                        "result_hex": actual.hex(),
                    }
                )
                results.append(actual)
                opcode_counts.append(counts["LoadUdfDataF64"])
            finally:
                registry.release(handle)

        if compiled_regions[0]["code_hash"] == compiled_regions[1]["code_hash"]:
            raise AssertionError("different verified Regions reused one code object")
        if results != [_affine(1.25), _changed_region(1.25)]:
            raise AssertionError("Region-driven results drifted")

        return {
            "node_id": runtime_context.get_node_id(),
            "actor_id": str(runtime_context.get_actor_id()),
            "worker_id": str(runtime_context.get_worker_id()),
            "pid": os.getpid(),
            "process_generation": registry.process_identity.generation,
            "registry_id": registry.registry_id,
            "regions": compiled_regions,
            "load_opcode_counts": opcode_counts,
        }


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray candidate cluster",
)
class RayCinderXScalarSlotSmokeTests(unittest.TestCase):
    def test_both_workers_execute_region_driven_cinderx_scalar_load(self) -> None:
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        cluster_epoch = os.environ.get("UDFJIT_CLUSTER_EPOCH", "")
        self.assertTrue(cluster_epoch, "UDFJIT_CLUSTER_EPOCH is required")
        ray.init(address="auto")
        actors = []
        try:
            alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
            self.assertEqual(len(alive_nodes), 3)
            head_nodes = [
                node
                for node in alive_nodes
                if node.get("NodeName") == "ray-head-driver"
            ]
            self.assertEqual(len(head_nodes), 1)
            self.assertEqual(head_nodes[0].get("Resources", {}).get("CPU", 0), 0)

            worker_nodes = sorted(
                (
                    node
                    for node in alive_nodes
                    if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(
                [node["NodeName"] for node in worker_nodes],
                ["ray-worker-1", "ray-worker-2"],
            )

            remote_probe = ray.remote(num_cpus=1)(_WorkerScalarSlotProbe)
            result_refs = []
            for node in worker_nodes:
                node_id = node["NodeID"]
                actor = remote_probe.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                ).remote()
                actors.append(actor)
                result_refs.append(actor.run.remote(cluster_epoch))

            reports = ray.get(result_refs)
            self.assertEqual(
                {report["node_id"] for report in reports},
                {node["NodeID"] for node in worker_nodes},
            )
            for report in reports:
                self.assertGreater(report["pid"], 0)
                self.assertTrue(report["actor_id"])
                self.assertTrue(report["worker_id"])
                self.assertTrue(report["process_generation"])
                self.assertEqual(report["load_opcode_counts"], [1, 1])
                regions = report["regions"]
                self.assertEqual(len(regions), 2)
                self.assertNotEqual(regions[0]["semantic_hash"], regions[1]["semantic_hash"])
                self.assertNotEqual(regions[0]["code_hash"], regions[1]["code_hash"])
            output_path = os.environ.get("UDFJIT_READINESS_REPORT_PATH", "")
            if output_path:
                manifest_path = Path(os.environ["UDFJIT_MANIFEST_PATH"])
                document = {
                    "schema_version": 1,
                    "phase": "readiness",
                    "run_id": os.environ["UDFJIT_RUN_ID"],
                    "cluster_epoch": cluster_epoch,
                    "manifest_sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                    "readiness": [
                        {
                            "node_id": report["node_id"],
                            "manifest_sha256": hashlib.sha256(
                                manifest_path.read_bytes()
                            ).hexdigest(),
                            "cinderx_compiled": True,
                            "process_generation": report["process_generation"],
                        }
                        for report in reports
                    ],
                }
                path = Path(output_path)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    json.dump(
                        document,
                        stream,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
        finally:
            for actor in actors:
                ray.kill(actor, no_restart=True)
            ray.shutdown()


if __name__ == "__main__":
    unittest.main()
