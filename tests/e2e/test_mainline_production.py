from __future__ import annotations

import hashlib
import json
import os
import unittest


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC003SystemTests(unittest.TestCase):
    def test_rfc003_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        from python_udf_jit.compiler.region import (
            form_semantic_region_graph,
        )
        from python_udf_jit.compiler.pipeline import compile_semantic
        from tests.semantic_cases import affine_semantic_module

        module = affine_semantic_module()
        graph = form_semantic_region_graph(module)
        driver_compiled = compile_semantic(module)
        payload = json.dumps(
            {
                "graph": graph.to_document(),
                "module": module.to_document(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        payload_hash = hashlib.sha256(payload).hexdigest()

        @ray.remote(num_cpus=0.01)
        def verify_on_worker(encoded: bytes):
            import hashlib as _hashlib
            import json as _json

            import ray as _ray

            from python_udf_jit.compiler.core_ir import (
                SemanticCoreModule as _SemanticCoreModule,
            )
            from python_udf_jit.compiler.pipeline import (
                compile_semantic as _compile_semantic,
            )
            from python_udf_jit.compiler.reference import (
                reference_execute_semantic as _reference_execute,
            )
            from python_udf_jit.compiler.region import (
                SemanticRegionGraph as _SemanticRegionGraph,
            )

            document = _json.loads(encoded)
            restored = _SemanticCoreModule.from_document(
                document["module"]
            )
            restored_graph = _SemanticRegionGraph.from_document(
                document["graph"],
                restored,
            )
            compiled = _compile_semantic(restored)
            return {
                "analysis_hash": compiled.analysis_summary.summary_hash,
                "graph_hash": restored_graph.semantic_hash,
                "module_hash": restored.semantic_hash,
                "node_id": _ray.get_runtime_context().get_node_id(),
                "payload_hash": _hashlib.sha256(encoded).hexdigest(),
                "provider_candidates": [
                    list(region.provider_candidates)
                    for region in compiled.region_graph.regions
                ],
                "result": _reference_execute(
                    compiled.core_module,
                    (4.0,),
                ),
            }

        ray.init(address="auto")
        try:
            alive = [node for node in ray.nodes() if node.get("Alive")]
            head = [
                node
                for node in alive
                if node.get("NodeName") == "ray-head-driver"
            ]
            workers = sorted(
                (
                    node
                    for node in alive
                    if node.get("NodeName")
                    in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(len(head), 1)
            self.assertEqual(
                head[0].get("Resources", {}).get("CPU", 0),
                0,
            )
            self.assertEqual(len(workers), 2)
            reports = ray.get(
                [
                    verify_on_worker.options(
                        scheduling_strategy=NodeAffinitySchedulingStrategy(
                            node_id=worker["NodeID"],
                            soft=False,
                        )
                    ).remote(payload)
                    for worker in workers
                ]
            )
        finally:
            ray.shutdown()

        self.assertEqual(
            {report["node_id"] for report in reports},
            {worker["NodeID"] for worker in workers},
        )
        self.assertEqual(
            {report["payload_hash"] for report in reports},
            {payload_hash},
        )
        self.assertEqual(
            {report["module_hash"] for report in reports},
            {module.semantic_hash},
        )
        self.assertEqual(
            {report["graph_hash"] for report in reports},
            {graph.semantic_hash},
        )
        self.assertEqual(
            {report["analysis_hash"] for report in reports},
            {driver_compiled.analysis_summary.summary_hash},
        )
        self.assertEqual(
            {report["result"] for report in reports},
            {11.0},
        )
        self.assertTrue(
            all(
                report["provider_candidates"]
                == [["scalar_cinderx"]]
                for report in reports
            )
        )


if __name__ == "__main__":
    unittest.main()
