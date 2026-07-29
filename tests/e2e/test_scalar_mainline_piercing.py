from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from python_udf_jit.diagnostics.evidence import (
    EvidenceContractError,
    EvidenceRun,
    aggregate_run_evidence,
    sanitize_event,
)
from tests.e2e.live_job import (
    _extract_events,
    _join_ray_task_attempts,
    _supported_attempt_evidence,
    _zero_row_runtime_counts,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
HEAD = "head-node"
WORKER_1 = "worker-node-1"
WORKER_2 = "worker-node-2"


def _nodes(*, worker_2_boot: str = "boot-worker-2") -> list[dict[str, object]]:
    return [
        {
            "role": "ray-head-driver",
            "node_id": HEAD,
            "container_boot_id": "boot-head",
        },
        {
            "role": "ray-worker-1",
            "node_id": WORKER_1,
            "container_boot_id": "boot-worker-1",
        },
        {
            "role": "ray-worker-2",
            "node_id": WORKER_2,
            "container_boot_id": worker_2_boot,
        },
    ]


def _base_evidence() -> dict[str, object]:
    snapshots = [
        {
            "phase": phase,
            "cluster_epoch": "epoch-1",
            "manifest_sha256": SHA_A,
            "nodes": _nodes(),
        }
        for phase in ("readiness", "qualification", "e2e")
    ]
    return {
        "run_id": "run-1",
        "cluster_epoch": "epoch-1",
        "manifest": {
            "candidate_manifest_sha256": SHA_A,
            "image_digest": f"sha256:{SHA_A}",
            "python_version": "3.14.3",
            "cinderx_commit": "abcd1234",
            "cinderx_base_image_digest": f"sha256:{SHA_B}",
            "cinderx_wheel_sha256": SHA_A,
            "soabi": "cpython-314-aarch64-linux-gnu",
            "daft_version": "0.7.2",
            "ray_version": "2.55.0",
            "pyarrow_version": "22.0.0",
            "udf_jit_wheel_sha256": SHA_B,
        },
        "phase_snapshots": snapshots,
        "topology": {
            "head_node_id": HEAD,
            "worker_node_ids": [WORKER_1, WORKER_2],
        },
        "readiness": [
            {
                "node_id": WORKER_1,
                "manifest_sha256": SHA_A,
                "cinderx_compiled": True,
            },
            {
                "node_id": WORKER_2,
                "manifest_sha256": SHA_A,
                "cinderx_compiled": True,
            },
        ],
        "qualification": [
            {
                "node_id": WORKER_1,
                "artifact_hash": SHA_B,
                "carrier_kind": "RaySwordfishActor",
                "carrier_config_hash": SHA_A,
                "process_generation": "qualification-generation-1",
                "compiled": True,
                "result_digest": SHA_A,
            },
            {
                "node_id": WORKER_2,
                "artifact_hash": SHA_B,
                "carrier_kind": "RaySwordfishActor",
                "carrier_config_hash": SHA_A,
                "process_generation": "qualification-generation-2",
                "compiled": True,
                "result_digest": SHA_A,
            },
        ],
        "scenarios": {
            "supported": {
                "completed": True,
                "off_result_digest": SHA_A,
                "auto_result_digest": SHA_A,
                "row_count": 4,
                "callable_calls": 0,
                "side_effect_count": 0,
            },
            "guard_miss": {
                "completed": True,
                "off_result_digest": SHA_A,
                "auto_result_digest": SHA_A,
                "row_count": 4,
                "off_callable_calls": 4,
                "auto_callable_calls": 4,
                "fallback_count": 4,
                "side_effect_count": 4,
                "semantic_execute_count": 0,
                "reason_code": "schema_mismatch",
            },
            "unsupported": {
                "completed": True,
                "off_result_digest": SHA_A,
                "auto_result_digest": SHA_A,
                "row_count": 4,
                "off_callable_calls": 4,
                "auto_callable_calls": 4,
                "side_effect_count": 4,
                "reason_code": "unsupported_opcode",
            },
            "mode_off": {
                "completed": True,
                "result_digest": SHA_A,
                "expected_result_digest": SHA_A,
                "reason_code": "mode_off",
            },
            "fingerprint_mismatch": {
                "completed": True,
                "result_digest": SHA_A,
                "expected_result_digest": SHA_A,
                "reason_code": "with_columns_fingerprint_mismatch",
            },
            "corrupt_artifact": {
                "completed": True,
                "result_digest": SHA_A,
                "expected_result_digest": SHA_A,
                "reason_code": "artifact_mismatch",
            },
            "zero_row": {
                "completed": True,
                "off_result_digest": SHA_A,
                "auto_result_digest": SHA_A,
                "row_count": 0,
                "callable_calls": 0,
                "descriptor_count": 0,
                "compile_count": 0,
                "hit_count": 0,
                "activity_event_count": 0,
            },
        },
    }


def _runtime_event(
    *,
    decision: str,
    timestamp_ns: int,
    partition_id: str,
    task_attempt: str,
    node_id: str = WORKER_1,
    process_generation: str = "natural-generation-1",
) -> dict[str, object]:
    return {
        "event_type": "runtime",
        "phase": "e2e",
        "scenario": "supported",
        "stage": "jit" if decision in {"compile", "hit"} else "execute",
        "decision": decision,
        "reason_code": (
            "cinderx_force_compile_verified"
            if decision == "compile"
            else "process_variant_cache"
            if decision == "hit"
            else "success"
        ),
        "cluster_epoch": "epoch-1",
        "run_id": "run-1",
        "role": "ray-worker-1" if node_id == WORKER_1 else "ray-worker-2",
        "node_id": node_id,
        "actor_id": "actor-1",
        "pid": 101,
        "process_generation": process_generation,
        "partition_id": partition_id,
        "task_attempt": task_attempt,
        "variant_key": SHA_A,
        "artifact_hash": SHA_B,
        "code_hash": SHA_A,
        "execution_mode": "cinderx-jit",
        "timestamp_ns": timestamp_ns,
    }


def _passing_events() -> list[dict[str, object]]:
    return [
        _runtime_event(
            decision="compile",
            timestamp_ns=1,
            partition_id="partition-1",
            task_attempt="attempt-0",
        ),
        _runtime_event(
            decision="semantic_execute",
            timestamp_ns=2,
            partition_id="partition-1",
            task_attempt="attempt-0",
        ),
        _runtime_event(
            decision="hit",
            timestamp_ns=3,
            partition_id="partition-2",
            task_attempt="attempt-0",
        ),
        _runtime_event(
            decision="semantic_execute",
            timestamp_ns=4,
            partition_id="partition-2",
            task_attempt="attempt-0",
        ),
    ]


class EvidenceAggregationTests(unittest.TestCase):
    def test_ray_state_join_accepts_only_unique_finished_attempt_zero(self) -> None:
        event = _passing_events()[0]
        event["partition_id"] = "ray-task-id-1"
        event["task_attempt"] = ""
        event = _extract_events(
            [
                json.dumps(
                    {
                        "runtime_task_id": "ray-task-id-1",
                        "runtime_node_id": event["node_id"],
                        "runtime_actor_id": event["actor_id"],
                        "runtime_worker_id": "ray-worker-id-1",
                        "pid": event["pid"],
                        "events": [event],
                    }
                )
            ]
        )[0]
        self.assertEqual(event["worker_id"], "ray-worker-id-1")
        record = SimpleNamespace(
            task_id="ray-task-id-1",
            attempt_number=0,
            state="FINISHED",
            actor_id=event["actor_id"],
            node_id=event["node_id"],
            worker_id=event["worker_id"],
            worker_pid=event["pid"],
        )
        running = SimpleNamespace(**vars(record))
        running.state = "RUNNING"
        state_module = ModuleType("ray.util.state")
        state_module.list_tasks = mock.Mock(side_effect=([running], [record]))
        util_module = ModuleType("ray.util")
        util_module.state = state_module
        ray_module = ModuleType("ray")
        ray_module.util = util_module

        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            joined = _join_ray_task_attempts(
                [event],
                wait_seconds=2.1,
                poll_seconds=0,
            )
        self.assertEqual(joined[0]["task_attempt"], "attempt-0")
        self.assertEqual(state_module.list_tasks.call_count, 2)
        self.assertTrue(
            all(
                isinstance(call.kwargs["timeout"], int)
                and 1 <= call.kwargs["timeout"] <= 2
                for call in state_module.list_tasks.call_args_list
            )
        )
        self.assertEqual(
            joined[0]["ray_state_exact_records"][0]["state"],
            "FINISHED",
        )

        record.attempt_number = 1
        state_module.list_tasks = lambda **_kwargs: [record]
        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            retry = _join_ray_task_attempts([event])
        self.assertEqual(retry[0]["task_attempt"], "")

        temporal_event = dict(event)
        temporal_event["partition_id"] = "runtime-thread-task-id"
        temporal_event["timestamp_ns"] = 2_000_500_000
        temporal_record = SimpleNamespace(
            **(
                vars(record)
                | {
                    "task_id": "physical-actor-task-id",
                    "attempt_number": 0,
                    "type": "ACTOR_TASK",
                    "start_time_ms": 2_000,
                    "end_time_ms": 2_001,
                }
            )
        )
        state_module.list_tasks = lambda **_kwargs: [temporal_record]
        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            temporal = _join_ray_task_attempts(
                [temporal_event],
                wait_seconds=2.1,
            )
        self.assertEqual(temporal[0]["partition_id"], "physical-actor-task-id")
        self.assertEqual(temporal[0]["task_attempt"], "attempt-0")
        self.assertEqual(
            temporal[0]["ray_state_join_strategy"],
            "unique_identity_time_window",
        )

        overlapping = SimpleNamespace(
            **(vars(temporal_record) | {"task_id": "overlapping-actor-task-id"})
        )
        state_module.list_tasks = lambda **_kwargs: [temporal_record, overlapping]
        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            ambiguous = _join_ray_task_attempts(
                [temporal_event],
                wait_seconds=2.1,
            )
        self.assertEqual(ambiguous[0]["task_attempt"], "")
        self.assertEqual(len(ambiguous[0]["ray_state_temporal_candidates"]), 2)
        self.assertEqual(
            len(ambiguous[0]["ray_state_candidate_attempt_records"]),
            2,
        )
        semantic = dict(ambiguous[0])
        semantic["decision"] = "semantic_execute"
        attempt_evidence = _supported_attempt_evidence([semantic])
        self.assertEqual(attempt_evidence["semantic_event_count"], 1)
        self.assertEqual(attempt_evidence["uncovered_event_count"], 0)
        self.assertEqual(len(attempt_evidence["records"]), 2)

    def test_ray_state_join_rejects_identity_without_task_or_time_window(self) -> None:
        event = _passing_events()[0]
        event["partition_id"] = "nested-runtime-task-id"
        event["task_attempt"] = ""
        record = SimpleNamespace(
            task_id="physical-plan-task-id",
            attempt_number=0,
            state="FINISHED",
            actor_id=event["actor_id"],
            node_id=event["node_id"],
            worker_pid=event["pid"],
        )
        state_module = ModuleType("ray.util.state")
        state_module.list_tasks = lambda **_kwargs: [record]
        util_module = ModuleType("ray.util")
        util_module.state = state_module
        ray_module = ModuleType("ray")
        ray_module.util = util_module

        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            joined = _join_ray_task_attempts([event], wait_seconds=2.1)
        self.assertEqual(joined[0]["task_attempt"], "")

    def test_ray_state_join_never_starts_a_query_after_budget_expires(self) -> None:
        event = _passing_events()[0]
        state_module = ModuleType("ray.util.state")
        state_module.list_tasks = mock.Mock()
        util_module = ModuleType("ray.util")
        util_module.state = state_module
        ray_module = ModuleType("ray")
        ray_module.util = util_module

        with mock.patch.dict(
            sys.modules,
            {"ray": ray_module, "ray.util": util_module, "ray.util.state": state_module},
        ):
            joined = _join_ray_task_attempts([event], wait_seconds=0.9)

        state_module.list_tasks.assert_not_called()
        self.assertEqual(joined[0]["ray_state_exact_records"], [])

    def test_zero_row_counts_are_derived_from_observed_runtime_events(
        self,
    ) -> None:
        events = [
            {"decision": "descriptor_bound"},
            {"decision": "compile"},
            {"decision": "hit"},
            {"decision": "semantic_execute"},
        ]

        self.assertEqual(
            _zero_row_runtime_counts(events),
            {
                "descriptor_count": 1,
                "compile_count": 1,
                "hit_count": 1,
                "activity_event_count": 4,
            },
        )
        self.assertEqual(
            _zero_row_runtime_counts([]),
            {
                "descriptor_count": 0,
                "compile_count": 0,
                "hit_count": 0,
                "activity_event_count": 0,
            },
        )

    def test_ae1_to_ae8_pass_and_keep_natural_coverage_separate(self) -> None:
        report = aggregate_run_evidence(_base_evidence(), _passing_events())

        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["natural_worker_coverage"], "1/2")
        self.assertEqual(report["qualified_worker_coverage"], "2/2")
        self.assertEqual(report["remote_partition_task_count"], 2)
        self.assertEqual(
            {item["role"] for item in report["fixed_topology"]},
            {"ray-head-driver", "ray-worker-1", "ray-worker-2"},
        )
        self.assertEqual(report["participating_worker_node_ids"], [WORKER_1])
        self.assertNotIn(HEAD, report["participating_worker_node_ids"])
        self.assertEqual(report["checks"]["supported_hit"], "pass")
        self.assertEqual(report["checks"]["guard_miss"], "pass")
        self.assertEqual(report["checks"]["unsupported"], "pass")

        dependency_rejection = _base_evidence()
        dependency_rejection["scenarios"]["unsupported"]["reason_code"] = (
            "unsupported_dependency"
        )
        dependency_report = aggregate_run_evidence(
            dependency_rejection,
            _passing_events(),
        )
        self.assertEqual(dependency_report["verdict"], "pass")
        self.assertEqual(dependency_report["checks"]["unsupported"], "pass")

        unregistered_rejection = _base_evidence()
        unregistered_rejection["scenarios"]["unsupported"]["reason_code"] = (
            "unregistered_reason"
        )
        unregistered_report = aggregate_run_evidence(
            unregistered_rejection,
            _passing_events(),
        )
        self.assertEqual(unregistered_report["verdict"], "fail")
        self.assertEqual(unregistered_report["checks"]["unsupported"], "fail")
        self.assertEqual(report["checks"]["fail_open"], "pass")
        self.assertEqual(report["checks"]["zero_row"], "pass")
        self.assertEqual(report["checks"]["evidence_identity"], "pass")
        self.assertEqual(report["checks"]["worker_pool_qualification"], "pass")
        self.assertTrue(report["result_summaries"]["supported"]["off_auto_equivalent"])

    def test_single_natural_worker_is_valid_but_two_qualification_workers_are_required(self) -> None:
        report = aggregate_run_evidence(_base_evidence(), _passing_events())
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["natural_worker_coverage"], "1/2")

        incomplete = _base_evidence()
        incomplete["qualification"] = incomplete["qualification"][:1]
        stopped = aggregate_run_evidence(incomplete, _passing_events())
        self.assertEqual(stopped["verdict"], "stop")
        self.assertEqual(stopped["checks"]["worker_pool_qualification"], "stop")

        reused_generation = _base_evidence()
        reused_generation["qualification"][1]["process_generation"] = (
            reused_generation["qualification"][0]["process_generation"]
        )
        stopped = aggregate_run_evidence(reused_generation, _passing_events())
        self.assertEqual(stopped["verdict"], "stop")

    def test_head_data_plane_event_is_failure(self) -> None:
        events = _passing_events()
        events[-1] = _runtime_event(
            decision="semantic_execute",
            timestamp_ns=4,
            partition_id="partition-2",
            task_attempt="task-2-attempt-0",
            node_id=HEAD,
        ) | {"role": "ray-head-driver"}
        report = aggregate_run_evidence(_base_evidence(), events)

        self.assertEqual(report["verdict"], "fail")
        self.assertIn("head_data_plane_event", report["reason_codes"])

    def test_missing_partition_or_one_remote_task_is_inconclusive_not_a_fake_pass(self) -> None:
        events = _passing_events()
        for event in events:
            event["partition_id"] = ""
            event["task_attempt"] = "same-task"
        report = aggregate_run_evidence(_base_evidence(), events)

        self.assertEqual(report["verdict"], "inconclusive")
        self.assertIn("partition_attempt_attribution_incomplete", report["reason_codes"])
        self.assertEqual(report["remote_partition_task_count"], 0)

        evidence = _base_evidence()

        def task_record(
            task_id: str,
            node_id: str,
            actor_id: str,
            worker_pid: int,
        ) -> dict[str, object]:
            return {
                "task_id": task_id,
                "job_id": "job-1",
                "attempt_number": 0,
                "state": "FINISHED",
                "type": "ACTOR_TASK",
                "actor_id": actor_id,
                "node_id": node_id,
                "worker_id": f"worker-{node_id}",
                "worker_pid": worker_pid,
                "name": "PhysicalScan->UDFProject",
                "parent_task_id": "parent-1",
                "start_time_ms": 1000,
                "end_time_ms": 1001,
            }

        evidence["supported_attempt_evidence"] = {
            "schema_version": 1,
            "semantic_event_count": 2,
            "uncovered_event_count": 0,
            "records": [
                task_record(
                    "task-1",
                    WORKER_1,
                    "actor-1",
                    101,
                ),
                task_record(
                    "task-2",
                    WORKER_1,
                    "actor-1",
                    101,
                ),
            ],
        }
        proven = aggregate_run_evidence(evidence, events)
        self.assertEqual(proven["verdict"], "pass", proven["reason_codes"])
        self.assertEqual(proven["remote_partition_task_count"], 2)

        retry_record = dict(evidence["supported_attempt_evidence"]["records"][0])
        retry_record["attempt_number"] = 1
        evidence["supported_attempt_evidence"]["records"].append(retry_record)
        retried = aggregate_run_evidence(evidence, events)
        self.assertEqual(retried["verdict"], "inconclusive")
        self.assertIn("partition_task_retry_observed", retried["reason_codes"])

        evidence["supported_attempt_evidence"]["records"].pop()
        evidence["supported_attempt_evidence"]["uncovered_event_count"] = 1
        uncovered = aggregate_run_evidence(evidence, events)
        self.assertEqual(uncovered["verdict"], "inconclusive")
        self.assertIn(
            "partition_attempt_attribution_incomplete",
            uncovered["reason_codes"],
        )

    def test_explicit_head_failure_is_not_masked_by_attempt_inconclusive(self) -> None:
        events = _passing_events()
        events[-1]["node_id"] = HEAD
        events[-1]["role"] = "ray-head-driver"
        events[-1]["partition_id"] = ""
        events[-1]["task_attempt"] = ""

        report = aggregate_run_evidence(_base_evidence(), events)

        self.assertEqual(report["verdict"], "fail")
        self.assertIn("head_data_plane_event", report["reason_codes"])
        self.assertIn(
            "partition_attempt_attribution_incomplete", report["reason_codes"]
        )

    def test_retry_or_attempt_ambiguity_is_inconclusive(self) -> None:
        events = _passing_events()
        retry = dict(events[-1])
        retry["task_attempt"] = "attempt-1"
        retry["timestamp_ns"] = 5
        events.append(retry)
        report = aggregate_run_evidence(_base_evidence(), events)

        self.assertEqual(report["verdict"], "inconclusive")
        self.assertIn("partition_attempt_not_unique", report["reason_codes"])

    def test_cross_phase_boot_drift_is_inconclusive(self) -> None:
        evidence = _base_evidence()
        evidence["phase_snapshots"][-1]["nodes"] = _nodes(
            worker_2_boot="replacement-boot"
        )
        report = aggregate_run_evidence(evidence, _passing_events())

        self.assertEqual(report["verdict"], "inconclusive")
        self.assertIn("phase_identity_drift", report["reason_codes"])

    def test_hit_before_compile_or_from_another_generation_fails(self) -> None:
        events = _passing_events()
        events[0], events[1] = events[1], events[0]
        events[0]["timestamp_ns"] = 1
        events[1]["timestamp_ns"] = 2
        report = aggregate_run_evidence(_base_evidence(), events)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("compile_hit_chain_invalid", report["reason_codes"])

        events = _passing_events()
        events[2]["process_generation"] = "other-generation"
        report = aggregate_run_evidence(_base_evidence(), events)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("compile_hit_chain_invalid", report["reason_codes"])

    def test_raw_event_permissions_whitelist_canary_and_cleanup_on_all_verdicts(self) -> None:
        canary = "CANARY-DO-NOT-LEAK-42"
        dependency_rejection = _passing_events()[0] | {
            "reason_code": "unsupported_dependency"
        }
        self.assertEqual(
            sanitize_event(dependency_rejection)["reason_code"],
            "unsupported_dependency",
        )
        for reason_code in ("compile_submitted", "compile_inflight"):
            with self.subTest(reason_code=reason_code):
                self.assertEqual(
                    sanitize_event(
                        dependency_rejection
                        | {"reason_code": reason_code}
                    )["reason_code"],
                    reason_code,
                )
        with self.assertRaisesRegex(EvidenceContractError, "event_reason_invalid"):
            sanitize_event(
                dependency_rejection | {"reason_code": "unregistered_reason"}
            )

        for expected in ("pass", "fail", "inconclusive"):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                evidence = _base_evidence()
                events = _passing_events()
                if expected == "fail":
                    events[-1]["node_id"] = HEAD
                    events[-1]["role"] = "ray-head-driver"
                elif expected == "inconclusive":
                    evidence["phase_snapshots"][-1]["nodes"] = _nodes(
                        worker_2_boot="replacement-boot"
                    )
                output_parent = Path(root) / "caller-output"
                output_parent.mkdir(mode=0o755)
                os.chmod(output_parent, 0o755)
                output = output_parent / f"{expected}.json"
                raw_root = Path(root) / "raw"
                raw_root.mkdir(mode=0o755)
                os.chmod(raw_root, 0o755)
                run = EvidenceRun(raw_root, f"run-{expected}")
                self.assertEqual(stat.S_IMODE(raw_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(run.raw_dir.stat().st_mode), 0o700)
                for event in events:
                    run.append_event(
                        event
                        | {
                            "source": canary,
                            "callable_repr": canary,
                            "exception_message": canary,
                            "business_value": canary,
                        }
                    )
                self.assertEqual(stat.S_IMODE(run.raw_file.stat().st_mode), 0o600)
                self.assertNotIn(canary, run.raw_file.read_text(encoding="utf-8"))

                report = run.finalize(evidence, output)

                self.assertEqual(report["verdict"], expected)
                self.assertFalse(run.raw_dir.exists())
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(output_parent.stat().st_mode),
                    0o755,
                )
                serialized = output.read_text(encoding="utf-8")
                self.assertNotIn(canary, serialized)
                self.assertNotIn("source", serialized)
                self.assertNotIn("callable_repr", serialized)
                self.assertNotIn("exception_message", serialized)
                self.assertNotIn("business_value", serialized)
                self.assertEqual(json.loads(serialized), report)


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 fixed three-node final candidate cluster",
)
class LiveScalarMainlinePiercingTests(unittest.TestCase):
    def test_live_evidence_is_supplied_by_the_external_harness(self) -> None:
        """The remote harness sets a 0600 report after executing real Daft jobs."""

        report_path = os.environ.get("UDFJIT_E2E_REPORT_PATH", "")
        self.assertTrue(report_path, "external harness must set UDFJIT_E2E_REPORT_PATH")
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        self.assertEqual(report["run_id"], os.environ["UDFJIT_RUN_ID"])
        self.assertEqual(report["cluster_epoch"], os.environ["UDFJIT_CLUSTER_EPOCH"])
        self.assertEqual(report["verdict"], "pass", report.get("reason_codes"))
        self.assertGreaterEqual(report["remote_partition_task_count"], 2)
        self.assertNotIn(report["head_node_id"], report["participating_worker_node_ids"])


if __name__ == "__main__":
    unittest.main()
