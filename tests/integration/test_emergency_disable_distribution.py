from __future__ import annotations

import json
import multiprocessing
import unittest
from multiprocessing.connection import Connection
from pathlib import Path

from python_udf_jit.governance.emergency import (
    EmergencyChannelLease,
    EmergencyControl,
    EmergencySnapshot,
    EmergencyTransitionError,
)


ROOT = Path(__file__).resolve().parents[2]


def _emergency_worker(connection: Connection) -> None:
    request = json.loads(connection.recv_bytes().decode("utf-8"))
    control = EmergencyControl()
    for raw in request["snapshots"]:
        control.apply(
            EmergencySnapshot(
                generation=raw["generation"],
                disabled=raw["disabled"],
                revoke_credentials_through=raw[
                    "revoke_credentials_through"
                ],
            )
        )
    decision = control.safe_point_decision(
        EmergencyChannelLease(
            minimum_generation=request["minimum_generation"],
            expires_at_ns=request["expires_at_ns"],
        ),
        now_ns=request["now_ns"],
    )
    try:
        control.apply(
            EmergencySnapshot(
                generation=decision.snapshot.generation + 1,
                disabled=False,
                revoke_credentials_through=0,
            )
        )
    except EmergencyTransitionError:
        relaxation_denied = True
    else:
        relaxation_denied = False
    connection.send_bytes(
        json.dumps(
            {
                "generation": decision.snapshot.generation,
                "optimized_execution_allowed": (
                    decision.optimized_execution_allowed
                ),
                "reason": decision.reason,
                "relaxation_denied": relaxation_denied,
                "evidence_scope": "local_process_contract",
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    connection.close()


def _run_emergency_worker(
    snapshots: list[dict[str, object]],
    *,
    minimum_generation: int,
    expires_at_ns: int,
    now_ns: int,
) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_emergency_worker, args=(child,))
    process.start()
    child.close()
    parent.send_bytes(
        json.dumps(
            {
                "snapshots": snapshots,
                "minimum_generation": minimum_generation,
                "expires_at_ns": expires_at_ns,
                "now_ns": now_ns,
            }
        ).encode("utf-8")
    )
    if not parent.poll(10):
        process.terminate()
        process.join(10)
        raise AssertionError("emergency worker response timeout")
    response = json.loads(parent.recv_bytes().decode("utf-8"))
    parent.close()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise AssertionError("emergency worker exit timeout")
    if process.exitcode != 0:
        raise AssertionError(f"emergency worker exit={process.exitcode}")
    return response


class EmergencyDisableDistributionIntegrationTests(unittest.TestCase):
    def test_two_workers_and_restart_observe_monotonic_disable(self) -> None:
        snapshots = [
            {
                "generation": 1,
                "disabled": False,
                "revoke_credentials_through": 0,
            },
            {
                "generation": 2,
                "disabled": True,
                "revoke_credentials_through": 1,
            },
        ]
        results = [
            _run_emergency_worker(
                snapshots,
                minimum_generation=2,
                expires_at_ns=200,
                now_ns=100,
            )
            for _ in range(3)
        ]

        for result in results:
            self.assertEqual(result["generation"], 2)
            self.assertFalse(result["optimized_execution_allowed"])
            self.assertEqual(result["reason"], "emergency_disabled")
            self.assertTrue(result["relaxation_denied"])
            self.assertEqual(
                result["evidence_scope"],
                "local_process_contract",
            )

    def test_stale_or_expired_control_channel_closes_optimization(self) -> None:
        control = EmergencyControl()
        control.apply(EmergencySnapshot(1, False, 0))

        stale = control.safe_point_decision(
            EmergencyChannelLease(
                minimum_generation=2,
                expires_at_ns=200,
            ),
            now_ns=100,
        )
        expired = control.safe_point_decision(
            EmergencyChannelLease(
                minimum_generation=1,
                expires_at_ns=99,
            ),
            now_ns=100,
        )

        self.assertFalse(stale.optimized_execution_allowed)
        self.assertEqual(stale.reason, "emergency_generation_stale")
        self.assertFalse(expired.optimized_execution_allowed)
        self.assertEqual(expired.reason, "emergency_channel_expired")

    def test_local_process_contract_is_not_blue98_or_multinode_evidence(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        prerequisite = matrix["release_prerequisites"][
            "emergency_disable_distribution"
        ]

        self.assertEqual(prerequisite["status"], "incomplete")
        self.assertEqual(prerequisite["gate_outcome"], "stop")
        self.assertEqual(
            prerequisite["local_evidence_scope"],
            "local_process_contract",
        )
        self.assertIsNone(prerequisite["blue98_evidence"])
        self.assertIsNone(prerequisite["physical_multinode_evidence"])


if __name__ == "__main__":
    unittest.main()
