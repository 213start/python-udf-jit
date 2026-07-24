from __future__ import annotations

import unittest
from pathlib import Path

from python_udf_jit.integration.daft_ray.topology import (
    ContractViolation,
    NodeObservation,
    load_topology_contract,
    validate_three_node_topology,
)


ROOT = Path(__file__).resolve().parents[2]


class ThreeNodeTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_topology_contract(
            ROOT / "config/ray-three-node-topology.json"
        )
        self.observations = (
            NodeObservation("head-id", "ray-head-driver", True, 0.0),
            NodeObservation("worker-1-id", "ray-worker-1", True, 2.0),
            NodeObservation("worker-2-id", "ray-worker-2", True, 2.0),
        )

    def test_contract_has_exactly_one_head_and_two_workers(self) -> None:
        self.assertEqual(3, len(self.contract.nodes))
        self.assertEqual(("ray-head-driver",), self.contract.head_services)
        self.assertEqual(
            ("ray-worker-1", "ray-worker-2"), self.contract.worker_services
        )
        self.assertEqual(0.0, self.contract.node("ray-head-driver").num_cpus)
        self.assertTrue(
            all(self.contract.node(role).num_cpus > 0 for role in self.contract.worker_services)
        )

    def test_exact_live_topology_is_accepted(self) -> None:
        snapshot = validate_three_node_topology(self.contract, self.observations)

        self.assertEqual("head-id", snapshot.head_node_id)
        self.assertEqual(
            frozenset(("worker-1-id", "worker-2-id")), snapshot.worker_node_ids
        )
        self.assertEqual(
            ({"node_id": "worker-1-id", "soft": False},
             {"node_id": "worker-2-id", "soft": False}),
            snapshot.readiness_targets,
        )

    def test_extra_node_and_head_cpu_are_rejected(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "exactly three"):
            validate_three_node_topology(
                self.contract,
                self.observations
                + (NodeObservation("extra", "ray-worker-3", True, 1.0),),
            )

        bad_head = (NodeObservation("head-id", "ray-head-driver", True, 1.0),) + self.observations[1:]
        with self.assertRaisesRegex(ContractViolation, "zero logical CPU"):
            validate_three_node_topology(self.contract, bad_head)


if __name__ == "__main__":
    unittest.main()
