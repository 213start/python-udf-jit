from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace


def _qualification_affine(value: float) -> float:
    return value * 2.0 + 3.0


def _qualification_positive(value: float) -> bool:
    return value > 0.0


def _qualification_identity(value: float) -> float:
    return float(value)


def _qualification_runtime_report(_value: float) -> str:
    """Unsupported probe that reads only value-free events in the same carrier."""

    import json as _json
    import os as _os

    import ray as _ray

    from python_udf_jit.diagnostics.report import DEFAULT_RUNTIME_REPORT

    runtime = _ray.get_runtime_context()
    pid = _os.getpid()
    run_id = _os.environ.get("UDFJIT_RUN_ID", "")
    events = [
        event
        for event in DEFAULT_RUNTIME_REPORT.snapshot()
        if event.run_id == run_id and event.process.pid == pid
    ]
    process_generation = events[-1].process.process_generation if events else ""
    return _json.dumps(
        {
            "actor_id": str(runtime.get_actor_id()),
            "artifact_hashes": sorted(
                {event.artifact_hash for event in events if event.artifact_hash}
            ),
            "code_hashes": sorted(
                {event.code_hash for event in events if event.code_hash}
            ),
            "decisions": [
                [event.stage, event.decision, event.reason_code]
                for event in events
            ],
            "node_id": runtime.get_node_id(),
            "pid": pid,
            "process_generation": process_generation,
            "variant_keys": sorted(
                {event.variant_key for event in events if event.variant_key}
            ),
            "worker_id": str(runtime.get_worker_id()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _QualificationSwordfishTask:
    """Driver-only task facade that exercises Daft's production Handle boundary."""

    def __init__(
        self,
        *,
        name,
        plan,
        config,
        partition_sets,
        context,
    ):
        self._name = name
        self._plan = plan
        self._config = config
        self._partition_sets = {
            key: [
                SimpleNamespace(object_ref=reference)
                for reference in references
            ]
            for key, references in partition_sets.items()
        }
        self._context = context

    def name(self):
        return self._name

    def plan(self):
        return self._plan

    def config(self):
        return self._config

    def psets(self):
        return self._partition_sets

    def context(self):
        return self._context


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray final candidate cluster",
)
class PerWorkerArtifactQualificationTests(unittest.TestCase):
    def test_rfc002_frontend_verifies_on_each_worker(self) -> None:
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        from python_udf_jit.compiler.abstract_interpreter import (
            analyze_function,
        )

        def branch(value):
            result = (value, "input")
            if value > 0.0:
                result = [value, "positive"]
            return result

        program = analyze_function(branch)
        payload = program.canonical_bytes()
        expected_hash = hashlib.sha256(payload).hexdigest()

        @ray.remote(num_cpus=0.01)
        def verify_on_worker(encoded: bytes) -> dict[str, str]:
            import hashlib as _hashlib
            import json as _json

            import ray as _ray

            from python_udf_jit.compiler.abstract_interpreter import (
                CapturedProgram as _CapturedProgram,
            )

            restored = _CapturedProgram.from_document(_json.loads(encoded))
            return {
                "program_hash": _hashlib.sha256(
                    restored.canonical_bytes()
                ).hexdigest(),
                "node_id": _ray.get_runtime_context().get_node_id(),
                "python_region_count": str(
                    len(restored.analysis.python_regions)
                ),
            }

        ray.init(address="auto")
        try:
            alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
            head = [
                node
                for node in alive_nodes
                if node.get("NodeName") == "ray-head-driver"
            ]
            workers = sorted(
                (
                    node
                    for node in alive_nodes
                    if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(len(head), 1)
            self.assertEqual(head[0].get("Resources", {}).get("CPU", 0), 0)
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
            self.assertEqual(
                {report["node_id"] for report in reports},
                {worker["NodeID"] for worker in workers},
            )
            self.assertEqual(
                {report["program_hash"] for report in reports},
                {expected_hash},
            )
            self.assertTrue(
                all(int(report["python_region_count"]) > 0 for report in reports)
            )
        finally:
            ray.shutdown()

    def test_same_production_plan_compiles_and_executes_on_each_worker(self) -> None:
        import daft
        import ray
        from daft.context import get_context
        from daft.daft import LocalPhysicalPlan
        from daft.runners.flotilla import (
            RaySwordfishActor,
            RaySwordfishActorHandle,
        )
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
        from python_udf_jit.integration.daft_ray.objectref_bridge import (
            driver_artifact_references,
        )

        self.assertEqual(os.environ.get("UDFJIT_MODE"), "auto")
        self.assertTrue(os.environ.get("UDFJIT_CLUSTER_EPOCH"))
        self.assertTrue(os.environ.get("UDFJIT_RUN_ID"))
        self.assertTrue(
            getattr(
                RaySwordfishActorHandle.submit_task,
                "__python_udf_jit_objectref_bridge__",
                False,
            )
        )
        ray.init(address="auto")
        actors = []
        try:
            alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
            head = [
                node
                for node in alive_nodes
                if node.get("NodeName") == "ray-head-driver"
            ]
            self.assertEqual(len(head), 1)
            self.assertEqual(head[0].get("Resources", {}).get("CPU", 0), 0)
            workers = sorted(
                (
                    node
                    for node in alive_nodes
                    if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(
                [node["NodeName"] for node in workers],
                ["ray-worker-1", "ray-worker-2"],
            )

            supported = daft.func(_qualification_affine)
            positive = daft.func(_qualification_positive)
            identity = daft.func(_qualification_identity)
            report_probe = daft.func(_qualification_runtime_report)
            qualification_inputs = [
                float(index) + 0.25 for index in range(64)
            ]
            dataframe = daft.from_pydict({"x": qualification_inputs})
            dataframe = dataframe.with_columns(
                {"y": supported(daft.col("x"))}
            )
            dataframe = dataframe.where(positive(daft.col("x")))
            dataframe = dataframe.select(
                "x",
                "y",
                z=identity(daft.col("x")),
            )
            dataframe = dataframe.with_columns(
                {
                    "qualification_report": report_probe(
                        daft.col("y")
                    )
                }
            )

            context = get_context()
            builder = dataframe._builder.optimize(context.daft_execution_config)
            plan = LocalPhysicalPlan.from_logical_plan_builder(builder._builder)
            runner = daft.get_or_create_runner()
            partition_sets = {}
            for partition_set_id, partition_set in (
                runner._part_set_cache.get_all_partition_sets().items()
            ):
                refs = []
                for materialized in partition_set.values():
                    partition = materialized.partition()
                    refs.append(
                        partition
                        if isinstance(partition, ray.ObjectRef)
                        else ray.put(partition)
                    )
                partition_sets[partition_set_id] = refs
            self.assertTrue(partition_sets)
            artifact_references = driver_artifact_references()
            self.assertTrue(artifact_references)
            self.assertTrue(
                all(
                    isinstance(record.reference, ray.ObjectRef)
                    for record in artifact_references
                )
            )
            reports = []
            for worker in workers:
                actor = RaySwordfishActor.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        node_id=worker["NodeID"],
                        soft=False,
                    )
                ).remote(
                    num_cpus=int(worker["Resources"]["CPU"]),
                    num_gpus=int(worker["Resources"].get("GPU", 0)),
                )
                actors.append(actor)
                handle = RaySwordfishActorHandle(actor)
                task_handle = handle.submit_task(
                    _QualificationSwordfishTask(
                        name=(
                            "udfjit-qualification-"
                            f"{worker['NodeName']}"
                        ),
                        plan=plan,
                        config=context.daft_execution_config,
                        partition_sets=partition_sets,
                        context={
                            "query_id": (
                                "udfjit-qualification-"
                                f"{worker['NodeName']}"
                            )
                        },
                    )
                )
                stream = task_handle.result_handle
                objects = ray.get(list(stream))
                self.assertGreaterEqual(len(objects), 2)
                partitions = objects[:-1]
                rows = []
                for partition in partitions:
                    document = partition.to_pydict()
                    rows.extend(
                        zip(
                            document["x"],
                            document["y"],
                            document["z"],
                            document["qualification_report"],
                            strict=True,
                        )
                    )
                self.assertEqual(
                    sorted((x, y, z) for x, y, z, _ in rows),
                    sorted(
                        (value, value * 2.0 + 3.0, value)
                        for value in qualification_inputs
                    ),
                )
                self.assertEqual(
                    sorted((x, y) for x, y, _, _ in rows),
                    sorted(
                        (value, value * 2.0 + 3.0)
                        for value in qualification_inputs
                    ),
                )
                self.assertEqual(len(rows), len(qualification_inputs))
                worker_reports = [
                    json.loads(value) for _, _, _, value in rows
                ]
                report = max(
                    worker_reports,
                    key=lambda value: len(value["decisions"]),
                )
                self.assertEqual(report["node_id"], worker["NodeID"])
                self.assertGreater(report["pid"], 0)
                self.assertTrue(report["actor_id"])
                self.assertTrue(report["worker_id"])
                self.assertTrue(report["process_generation"])
                self.assertEqual(len(report["artifact_hashes"]), 1)
                self.assertEqual(len(report["code_hashes"]), 1)
                self.assertEqual(len(report["variant_keys"]), 1)
                decisions = report["decisions"]
                self.assertIn(
                    ["jit", "compile", "cinderx_force_compile_verified"],
                    decisions,
                )
                self.assertIn(
                    ["jit", "hit", "process_variant_cache"],
                    decisions,
                )
                self.assertEqual(
                    sum(
                        decision[1] == "semantic_execute"
                        for decision in decisions
                    )
                    > 0,
                    True,
                )
                report["carrier_config_hash"] = hashlib.sha256(
                    json.dumps(
                        {
                            "actor_class": "RaySwordfishActor",
                            "num_cpus": int(worker["Resources"]["CPU"]),
                            "num_gpus": int(worker["Resources"].get("GPU", 0)),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest()
                report["qualification_result_digest"] = hashlib.sha256(
                    json.dumps(
                        sorted(
                            (float(x).hex(), float(y).hex())
                            for x, y, _, _ in rows
                        ),
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest()
                reports.append(report)

            self.assertEqual(len(reports), 2)
            self.assertEqual(reports[0]["artifact_hashes"], reports[1]["artifact_hashes"])
            self.assertEqual(reports[0]["code_hashes"], reports[1]["code_hashes"])
            self.assertNotEqual(reports[0]["variant_keys"], reports[1]["variant_keys"])
            self.assertNotEqual(
                reports[0]["process_generation"], reports[1]["process_generation"]
            )
            output_path = os.environ.get("UDFJIT_QUALIFICATION_REPORT_PATH", "")
            if output_path:
                document = {
                    "schema_version": 1,
                    "phase": "qualification",
                    "run_id": os.environ["UDFJIT_RUN_ID"],
                    "cluster_epoch": os.environ["UDFJIT_CLUSTER_EPOCH"],
                    "qualification": [
                        {
                            "node_id": report["node_id"],
                            "artifact_hash": report["artifact_hashes"][0],
                            "carrier_kind": "RaySwordfishActor",
                            "carrier_config_hash": report["carrier_config_hash"],
                            "process_generation": report["process_generation"],
                            "compiled": True,
                            "result_digest": report[
                                "qualification_result_digest"
                            ],
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
