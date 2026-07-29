from __future__ import annotations

import json
import unittest
from pathlib import Path

from python_udf_jit.integration.daft_ray.topology import (
    ContractViolation,
    assert_data_plane_isolation,
)


ROOT = Path(__file__).resolve().parents[2]


class DriverWorkerIsolationTests(unittest.TestCase):
    def test_compose_exposes_only_loopback_dashboard_jobs_port(self) -> None:
        compose = (ROOT / "docker/scalar-piercing/compose.yaml").read_text()
        entrypoint = (ROOT / "docker/scalar-piercing/entrypoint.sh").read_text()
        seccomp = json.loads(
            (
                ROOT / "docker/scalar-piercing/seccomp-wx.json"
            ).read_text(encoding="utf-8")
        )

        self.assertIn('version: "3.7"', compose)
        self.assertNotIn("name: scalar-piercing", compose)
        published = [line.strip() for line in compose.splitlines() if "127.0.0.1:" in line]
        self.assertEqual(['- "127.0.0.1:8265:8265"'], published)
        self.assertNotIn("10001:", compose)
        self.assertNotIn("6379:", compose)
        self.assertIn("internal: true", compose)
        self.assertIn("dashboard-loopback", compose)
        self.assertIn(
            "${SCALAR_PIERCING_DATA_PLANE_SUBNET:-172.23.240.0/24}",
            compose,
        )
        self.assertIn(
            "${SCALAR_PIERCING_DASHBOARD_SUBNET:-172.23.241.0/24}",
            compose,
        )
        head_section, worker_section = compose.split("  ray-worker-1:", maxsplit=1)
        self.assertIn("dashboard-loopback", head_section)
        self.assertNotIn("dashboard-loopback", worker_section.split("networks:", maxsplit=1)[0])
        self.assertIn("ray-head-data-plane", head_section)
        self.assertIn("aliases:", head_section)
        self.assertIn("RAY_HEAD_DATA_PLANE_HOST: ray-head-data-plane", compose)
        self.assertIn("RAY_AUTH_MODE: token", compose)
        self.assertIn("RAY_AUTH_TOKEN_PATH: /run/secrets/ray-auth-token", compose)
        self.assertIn('UDFJIT_MODE: "${UDFJIT_MODE:-off}"', compose)
        self.assertIn("UDFJIT_CLUSTER_EPOCH:", compose)
        self.assertIn("UDFJIT_RUN_ID:", compose)
        self.assertGreaterEqual(
            compose.count("org.python-udf-jit.run-id:"),
            3,
        )
        self.assertGreaterEqual(
            compose.count("org.python-udf-jit.cluster-epoch:"),
            3,
        )
        self.assertIn("UDFJIT_MANIFEST_PATH:", compose)
        self.assertNotIn("RAY_AUTH_TOKEN:", compose)
        self.assertIn("mode: 0400", compose)
        self.assertIn("security_opt:", compose)
        self.assertIn(
            "seccomp=${SCALAR_PIERCING_SECCOMP_PROFILE:"
            "-./seccomp-wx.json}",
            compose,
        )
        self.assertEqual(
            seccomp,
            {
                "defaultAction": "SCMP_ACT_ALLOW",
                "architectures": ["SCMP_ARCH_AARCH64"],
                "syscalls": [
                    {
                        "names": ["mmap", "mprotect", "pkey_mprotect"],
                        "action": "SCMP_ACT_ERRNO",
                        "args": [
                            {
                                "index": 2,
                                "value": 6,
                                "valueTwo": 6,
                                "op": "SCMP_CMP_MASKED_EQ",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertNotIn("export RAY_AUTH_TOKEN", entrypoint)
        self.assertIn('--node-ip-address="$head_ip"', entrypoint)
        self.assertIn('socket.create_connection((host, 6379), 1)', entrypoint)
        self.assertIn('--address="$data_plane_host:6379"', entrypoint)

    def test_compose_declares_head_zero_cpu_and_two_workers(self) -> None:
        compose = (ROOT / "docker/scalar-piercing/compose.yaml").read_text()
        topology = (ROOT / "config/ray-three-node-topology.json").read_text()

        self.assertIn("ray-head-driver:", compose)
        self.assertIn("ray-worker-1:", compose)
        self.assertIn("ray-worker-2:", compose)
        self.assertIn('"num_cpus": 0', topology)
        self.assertIn('"driver_location": "ray-head-driver"', topology)

    def test_entrypoint_clears_base_image_autojit_policy(self) -> None:
        entrypoint = (
            ROOT / "docker/scalar-piercing/entrypoint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("unset PYTHONJITAUTO", entrypoint)
        self.assertIn("unset PYTHONJITDISABLE", entrypoint)
        self.assertIn("export PYTHONJIT=1", entrypoint)

    def test_data_plane_events_may_only_come_from_worker_nodes(self) -> None:
        assert_data_plane_isolation(
            head_node_id="head-id",
            worker_node_ids={"worker-1-id", "worker-2-id"},
            event_node_ids=["worker-2-id"],
        )

        with self.assertRaisesRegex(ContractViolation, "Head/Driver"):
            assert_data_plane_isolation(
                head_node_id="head-id",
                worker_node_ids={"worker-1-id", "worker-2-id"},
                event_node_ids=["head-id"],
            )


if __name__ == "__main__":
    unittest.main()
