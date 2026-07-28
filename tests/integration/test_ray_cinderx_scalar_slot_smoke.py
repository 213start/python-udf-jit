from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path


class _WorkerScalarSlotProbe:
    def run(self, cluster_epoch: str) -> dict[str, object]:
        import hashlib
        import sysconfig

        import cinderx.jit
        import ray

        from python_udf_jit.compiler.capture import FallbackIdentity
        from python_udf_jit.compiler.region import (
            form_semantic_region_graph,
        )
        from python_udf_jit.protocol.artifact import build_artifact
        from python_udf_jit.provider.scalar_python.executor import (
            CinderXScalarProviderFactory,
        )
        from python_udf_jit.runtime.descriptors import (
            scalar_input_spec,
            scalar_output_spec,
        )
        from python_udf_jit.runtime.layout import (
            SCALAR_SLOT_ABI_VERSION,
            normalize_scalar_value,
        )
        from python_udf_jit.runtime.continuation import CommitBoundary
        from python_udf_jit.runtime.variant import (
            VariantKey,
            WorkerProcessKey,
        )
        from tests.unit.provider.scalar_python.test_scalar_matrix import (
            _LOGICAL_TYPES,
            _branch_module,
            _identity_module,
        )

        runtime_context = ray.get_runtime_context()
        node_id = runtime_context.get_node_id()
        actor_id = str(runtime_context.get_actor_id())
        worker_id = str(runtime_context.get_worker_id())
        pid = os.getpid()
        process_generation = hashlib.sha256(
            f"{cluster_epoch}:{node_id}:{actor_id}:{worker_id}:{pid}".encode(
                "ascii"
            )
        ).hexdigest()
        process_key = WorkerProcessKey(
            cluster_epoch,
            node_id,
            f"{actor_id}:{worker_id}",
            pid,
            process_generation,
        )
        factory = CinderXScalarProviderFactory()
        reports = []
        variants = []

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("ascii")).hexdigest()

        def compile_variant(
            scalar_type: str,
            *,
            nullable: bool,
            module,
            label: str,
        ):
            graph = form_semantic_region_graph(module)
            input_spec = scalar_input_spec(
                scalar_type,
                nullable=nullable,
            )
            output_spec = scalar_output_spec(
                scalar_type,
                nullable=nullable,
            )
            artifact = build_artifact(
                module,
                graph,
                FallbackIdentity(
                    "tests.ray_cinderx_scalar_matrix",
                    label,
                    module.function_id,
                ),
                input_access_specs=(input_spec,),
                output_access_spec=output_spec,
            )
            key = VariantKey(
                process=process_key,
                artifact_content_sha256=artifact.content_sha256,
                semantic_hash=module.semantic_hash,
                schema_fingerprint=digest(
                    f"{scalar_type}:{nullable}"
                ),
                callable_code_sha256=module.function_id,
                artifact_manifest_sha256=digest("formal-artifact-1.0"),
                experiment_manifest_sha256=digest(cluster_epoch),
                adapter_abi=1,
                runtime_abi=1,
                scalar_slot_abi=SCALAR_SLOT_ABI_VERSION,
                cpython_cinderx_soabi=str(
                    sysconfig.get_config_var("SOABI")
                ),
                cpu_features=("scalar",),
            )
            variant = factory.compile(artifact, key)
            variants.append(variant)
            return variant

        representatives = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": 1 << 40,
            "float32": 1.1,
            "float64": -3.5,
        }
        hir_type_names = {
            "bool": "Bool",
            "int32": "I32",
            "int64": "I64",
            "float32": "F32",
            "float64": "F64",
        }
        try:
            for scalar_type, value in representatives.items():
                module = _identity_module(
                    _LOGICAL_TYPES[scalar_type],
                    nullable=True,
                )
                variant = compile_variant(
                    scalar_type,
                    nullable=True,
                    module=module,
                    label=f"identity_{scalar_type}",
                )
                actual = variant.execute(
                    value,
                    boundary=CommitBoundary(),
                )
                expected = normalize_scalar_value(
                    value,
                    scalar_type,
                    nullable=True,
                )
                if (
                    type(expected) is float
                    and actual.hex() != expected.hex()
                ) or (
                    type(expected) is not float
                    and (
                        actual != expected
                        or type(actual) is not type(expected)
                    )
                ):
                    raise AssertionError(
                        (scalar_type, actual, expected)
                    )
                if (
                    variant.execute(
                        None,
                        boundary=CommitBoundary(),
                    )
                    is not None
                ):
                    raise AssertionError(
                        f"{scalar_type} null value was not preserved"
                    )
                counts = cinderx.jit.get_function_hir_opcode_counts(
                    variant.compiled.jit_function
                )
                hir_type = hir_type_names[scalar_type]
                if (
                    counts.get(f"LoadUdfData{hir_type}", 0) != 1
                    or counts.get(f"StoreUdfData{hir_type}", 0) != 1
                    or counts.get("IsUdfDataNull", 0) != 1
                    or counts.get("StoreUdfDataNull", 0) != 1
                ):
                    raise AssertionError(
                        f"{scalar_type} HIR evidence drift: {counts}"
                    )
                input_descriptor = variant.registry.descriptor(
                    variant.input_handle
                )
                output_descriptor = variant.registry.descriptor(
                    variant.output_handle
                )
                if (
                    input_descriptor.scalar_type != scalar_type
                    or output_descriptor.scalar_type != scalar_type
                    or not input_descriptor.nullable
                    or not output_descriptor.nullable
                    or input_descriptor.epoch != cluster_epoch
                    or output_descriptor.epoch != cluster_epoch
                    or input_descriptor.process
                    != variant.registry.process_identity
                    or output_descriptor.process
                    != variant.registry.process_identity
                ):
                    raise AssertionError("typed descriptor identity drift")
                reports.append(
                    {
                        "scalar_type": scalar_type,
                        "semantic_hash": variant.compiled.semantic_hash,
                        "code_hash": variant.code_hash,
                        "intrinsic_counts": counts,
                    }
                )

            branch = compile_variant(
                "int64",
                nullable=False,
                module=_branch_module(),
                label="int64_branch",
            )
            branch_results = [
                branch.execute(-9),
                branch.execute(7),
            ]
            if branch_results != [9, 7]:
                raise AssertionError(
                    f"branch result drift: {branch_results}"
                )
            branch_counts = cinderx.jit.get_function_hir_opcode_counts(
                branch.compiled.jit_function
            )
            if (
                branch_counts.get("LoadUdfDataI64", 0) != 1
                or branch_counts.get("StoreUdfDataI64", 0) != 1
                or branch_counts.get("CondBranch", 0) < 1
            ):
                raise AssertionError(
                    f"branch HIR evidence drift: {branch_counts}"
                )
        finally:
            for variant in reversed(variants):
                variant.close()

        if len({report["code_hash"] for report in reports}) != 5:
            raise AssertionError("physical scalar types reused one code hash")

        return {
            "node_id": node_id,
            "actor_id": actor_id,
            "worker_id": worker_id,
            "pid": pid,
            "process_generation": process_generation,
            "scalar_types": reports,
            "branch_results": branch_results,
            "branch_counts": branch_counts,
        }


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray candidate cluster",
)
class RayCinderXScalarSlotSmokeTests(unittest.TestCase):
    def test_both_workers_execute_full_cinderx_scalar_matrix(self) -> None:
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
                scalar_types = report["scalar_types"]
                self.assertEqual(
                    {
                        item["scalar_type"]
                        for item in scalar_types
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
                            for item in scalar_types
                        }
                    ),
                    5,
                )
                self.assertEqual(report["branch_results"], [9, 7])
                self.assertGreaterEqual(
                    report["branch_counts"].get("CondBranch", 0),
                    1,
                )
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
