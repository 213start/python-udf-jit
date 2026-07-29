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


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC004SystemTests(unittest.TestCase):
    def test_rfc004_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        from python_udf_jit.compiler.capture import CaptureRequest, capture
        from python_udf_jit.compiler.pipeline import compile_semantic
        from python_udf_jit.integration.daft_ray.carrier import (
            ProductionCarrierState,
        )
        from python_udf_jit.protocol.artifact import build_artifact
        from python_udf_jit.protocol.codec import encode_artifact

        def affine(value):
            return value * 2.0 + 3.0

        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        encoded = encode_artifact(
            build_artifact(
                compiled.core_module,
                compiled.region_graph,
                captured.fallback_identity,
            )
        )
        expected_hash = hashlib.sha256(encoded).hexdigest()

        @ray.remote(num_cpus=0.01)
        def load_twice_on_worker(carrier):
            import os as _os

            import ray as _ray

            from python_udf_jit.compiler.reference import (
                reference_execute_semantic as _reference_execute,
            )
            from python_udf_jit.protocol.loader import (
                ArtifactLoader as _ArtifactLoader,
                LoaderNamespace as _LoaderNamespace,
            )

            context = _ray.get_runtime_context()
            loader = _ArtifactLoader()
            namespace = _LoaderNamespace(
                "rfc004-system-job",
                "trusted-job",
                f"{_os.getpid()}:{context.get_worker_id()}",
            )
            first = loader.load(carrier.handle, namespace)
            second = loader.load(carrier.handle, namespace)
            return {
                "artifact_hash": first.content_sha256,
                "cache_entries": loader.positive_entry_count,
                "identity_reused": first is second,
                "node_id": context.get_node_id(),
                "result": _reference_execute(
                    first.semantic_core_module,
                    (4.0,),
                ),
            }

        ray.init(
            address="auto",
            runtime_env={
                "env_vars": {
                    "DAFT_PROGRESS_BAR": "0",
                    "RAY_TQDM": "0",
                }
            },
        )
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
            reference = ray.put(encoded)
            carrier = ProductionCarrierState.placeholder(
                "rfc004-system",
                "a" * 64,
            ).finalize(
                encoded,
                inline_threshold=0,
                publisher=lambda _payload: reference,
            )
            reports = ray.get(
                [
                    load_twice_on_worker.options(
                        scheduling_strategy=NodeAffinitySchedulingStrategy(
                            node_id=worker["NodeID"],
                            soft=False,
                        )
                    ).remote(carrier)
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
            {report["artifact_hash"] for report in reports},
            {expected_hash},
        )
        self.assertEqual(
            {report["result"] for report in reports},
            {11.0},
        )
        self.assertTrue(
            all(
                report["cache_entries"] == 1
                and report["identity_reused"]
                for report in reports
            )
        )


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC005SystemTests(unittest.TestCase):
    def test_rfc005_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        @ray.remote(num_cpus=0.01)
        def physicalize_on_worker():
            import dataclasses as _dataclasses

            import ray as _ray

            from python_udf_jit.runtime.descriptors import (
                admit_access_spec as _admit_access_spec,
                scalar_input_spec as _input_spec,
                scalar_output_spec as _output_spec,
            )
            from python_udf_jit.runtime.layout import (
                SUPPORTED_SCALAR_TYPES as _TYPES,
            )
            from python_udf_jit.runtime.physicalize import (
                ScalarPhysicalizer as _ScalarPhysicalizer,
            )

            values = {
                "bool": True,
                "int32": -(1 << 31),
                "int64": (1 << 63) - 1,
                "float32": 1.25,
                "float64": -0.0,
            }
            physicalizer = _ScalarPhysicalizer(
                epoch="rfc005-system",
            )
            fingerprints = {}
            outputs = {}
            layouts = set()
            for scalar_type in _TYPES:
                with physicalizer.open_call(
                    _input_spec(
                        scalar_type,
                        nullable=True,
                    ),
                    _output_spec(
                        scalar_type,
                        nullable=True,
                    ),
                    values[scalar_type],
                ) as frame:
                    loaded = frame.load_input()
                    frame.stage_output(loaded)
                    outputs[scalar_type] = frame.publish_output()
                    fingerprints[scalar_type] = (
                        frame.descriptor_set.input_descriptor.layout_fingerprint
                    )
                    layouts.add(
                        frame.descriptor_set.input_descriptor.layout_kind
                    )
            active = physicalizer.active_frame_count
            physicalizer.close()
            scalar = _input_spec("float64", nullable=False)
            rejections = {
                layout: _admit_access_spec(
                    _dataclasses.replace(
                        scalar,
                        layout_kind=layout,
                    )
                ).reason
                for layout in (
                    "arrow_array",
                    "batch_view",
                    "unknown",
                )
            }
            return {
                "active_frames": active,
                "fingerprints": fingerprints,
                "layouts": sorted(layouts),
                "node_id": (
                    _ray.get_runtime_context().get_node_id()
                ),
                "outputs": outputs,
                "rejections": rejections,
            }

        ray.init(address="auto")
        try:
            alive = [
                node
                for node in ray.nodes()
                if node.get("Alive")
            ]
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
                    physicalize_on_worker.options(
                        scheduling_strategy=(
                            NodeAffinitySchedulingStrategy(
                                node_id=worker["NodeID"],
                                soft=False,
                            )
                        )
                    ).remote()
                    for worker in workers
                ]
            )
        finally:
            ray.shutdown()

        self.assertEqual(
            {report["node_id"] for report in reports},
            {worker["NodeID"] for worker in workers},
        )
        self.assertTrue(
            all(
                set(report["outputs"])
                == {
                    "bool",
                    "int32",
                    "int64",
                    "float32",
                    "float64",
                }
                and report["layouts"] == ["scalar_slot"]
                and report["active_frames"] == 0
                and report["rejections"]
                == {
                    "arrow_array": (
                        "arrow_layout_not_implemented"
                    ),
                    "batch_view": (
                        "batch_layout_not_implemented"
                    ),
                    "unknown": "unknown_layout_kind",
                }
                for report in reports
            )
        )
        self.assertEqual(
            reports[0]["fingerprints"],
            reports[1]["fingerprints"],
        )
        self.assertEqual(
            len(set(reports[0]["fingerprints"].values())),
            5,
        )


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC006SystemTests(unittest.TestCase):
    def test_rfc006_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        from tests.integration.test_ray_cinderx_scalar_slot_smoke import (
            _WorkerScalarSlotProbe,
        )

        cluster_epoch = os.environ.get("UDFJIT_CLUSTER_EPOCH", "")
        self.assertTrue(cluster_epoch)
        ray.init(address="auto")
        actors = []
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
            self.assertEqual(
                [node["NodeName"] for node in workers],
                ["ray-worker-1", "ray-worker-2"],
            )
            remote_probe = ray.remote(num_cpus=1)(
                _WorkerScalarSlotProbe
            )
            references = []
            for worker in workers:
                actor = remote_probe.options(
                    scheduling_strategy=(
                        NodeAffinitySchedulingStrategy(
                            node_id=worker["NodeID"],
                            soft=False,
                        )
                    )
                ).remote()
                actors.append(actor)
                references.append(
                    actor.run.remote(cluster_epoch)
                )
            reports = ray.get(references)
        finally:
            for actor in actors:
                ray.kill(actor, no_restart=True)
            ray.shutdown()

        self.assertEqual(
            {report["node_id"] for report in reports},
            {worker["NodeID"] for worker in workers},
        )
        for report in reports:
            self.assertEqual(
                {
                    item["scalar_type"]
                    for item in report["scalar_types"]
                },
                {
                    "bool",
                    "int32",
                    "int64",
                    "float32",
                    "float64",
                },
            )
            self.assertEqual(
                len(
                    {
                        item["code_hash"]
                        for item in report["scalar_types"]
                    }
                ),
                5,
            )
            self.assertEqual(report["branch_results"], [9, 7])
            self.assertGreaterEqual(
                report["branch_counts"].get("CondBranch", 0),
                1,
            )


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC007SystemTests(unittest.TestCase):
    def test_rfc007_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        @ray.remote(num_cpus=1)
        def qualify_variant_runtime(cluster_epoch):
            import os

            from python_udf_jit.runtime.variant import (
                VariantKey,
                WorkerProcessKey,
            )
            from python_udf_jit.runtime.variant_manager import (
                VariantManager,
                VariantNamespace,
            )

            context = ray.get_runtime_context()
            process = WorkerProcessKey(
                cluster_epoch,
                context.get_node_id(),
                context.get_worker_id(),
                os.getpid(),
                f"generation-{os.getpid()}",
            )
            key = VariantKey(
                process,
                "0" * 64,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                1,
                1,
                1,
                "cpython-314-aarch64-linux-gnu",
                ("asimd",),
            )
            manager = VariantManager(
                process=process,
                namespace=VariantNamespace("live-job", "default"),
                max_variants=2,
                max_code_bytes=2,
            )
            try:
                first = manager.resolve(key, lambda: "compiled")
                manager.drain()
                second = manager.resolve(key, lambda: "wrong")
                return {
                    "first": first.kind,
                    "second": second.kind,
                    "node_id": context.get_node_id(),
                    "process_generation": process.process_generation,
                }
            finally:
                manager.close()

        cluster_epoch = os.environ["UDFJIT_CLUSTER_EPOCH"]
        ray.init(address="auto")
        try:
            workers = sorted(
                (
                    node
                    for node in ray.nodes()
                    if node.get("Alive")
                    and node.get("NodeName")
                    in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            reports = ray.get(
                [
                    qualify_variant_runtime.options(
                        scheduling_strategy=(
                            NodeAffinitySchedulingStrategy(
                                node_id=worker["NodeID"],
                                soft=False,
                            )
                        )
                    ).remote(cluster_epoch)
                    for worker in workers
                ]
            )
        finally:
            ray.shutdown()

        self.assertEqual(len(reports), 2)
        self.assertEqual(
            {report["node_id"] for report in reports},
            {worker["NodeID"] for worker in workers},
        )
        self.assertEqual(
            {(report["first"], report["second"]) for report in reports},
            {("compile_pending", "hit")},
        )
        self.assertEqual(
            len(
                {
                    report["process_generation"]
                    for report in reports
                }
            ),
            2,
        )


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class RFC008SystemTests(unittest.TestCase):
    def test_rfc008_system_contract(self):
        import ray
        from ray.util.scheduling_strategies import (
            NodeAffinitySchedulingStrategy,
        )

        @ray.remote(num_cpus=1)
        def governance_probe():
            from python_udf_jit.governance.policy import PolicySnapshot
            from python_udf_jit.governance.telemetry import (
                AsyncTelemetry,
                GovernanceEvent,
            )

            policy = PolicySnapshot.mainline(
                version="frozen-live",
                budgets={
                    "compile_concurrency": 1,
                    "variant_limit": 8,
                },
                observe_shadow_compile=True,
                rollout_authorized=True,
            )
            delivered = []
            telemetry = AsyncTelemetry(delivered.append, capacity=4)
            try:
                accepted = telemetry.try_emit(
                    GovernanceEvent(
                        run_id=os.environ["UDFJIT_RUN_ID"],
                        job_id="live-job",
                        tenant_id="default",
                        policy_sha256=policy.sha256,
                        stage="execute",
                        decision="hit",
                        reason_code="variant_cache_hit",
                        source_identity="b" * 64,
                        artifact_sha256="c" * 64,
                        variant_sha256="d" * 64,
                    )
                )
                telemetry.flush()
                counters = telemetry.counters()
            finally:
                telemetry.close()
            return {
                "accepted": accepted,
                "backend_failures": counters.backend_failures,
                "delivered": counters.delivered,
                "node_id": ray.get_runtime_context().get_node_id(),
                "policy_sha256": policy.sha256,
                "vector_enabled": policy.provider_flags["vector"],
            }

        ray.init(address="auto")
        try:
            workers = [
                node
                for node in ray.nodes()
                if node.get("Alive")
                and node.get("NodeName")
                in {"ray-worker-1", "ray-worker-2"}
            ]
            reports = ray.get(
                [
                    governance_probe.options(
                        scheduling_strategy=(
                            NodeAffinitySchedulingStrategy(
                                node_id=worker["NodeID"],
                                soft=False,
                            )
                        )
                    ).remote()
                    for worker in workers
                ]
            )
        finally:
            ray.shutdown()

        by_node = {report["node_id"]: report for report in reports}
        self.assertEqual(
            set(by_node),
            {worker["NodeID"] for worker in workers},
        )
        self.assertEqual(
            {report["policy_sha256"] for report in reports},
            {reports[0]["policy_sha256"]},
        )
        self.assertTrue(all(report["accepted"] for report in reports))
        self.assertTrue(
            all(report["delivered"] == 1 for report in reports)
        )
        self.assertTrue(
            all(report["backend_failures"] == 0 for report in reports)
        )
        self.assertFalse(any(report["vector_enabled"] for report in reports))


if __name__ == "__main__":
    unittest.main()
