from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.system.probe_environment import (
    _published_ports,
    _scan_files,
)


class EnvironmentProbeTests(unittest.TestCase):
    def test_exact_three_container_port_observation_is_normalized(self) -> None:
        documents = [
            {
                "NetworkSettings": {
                    "Ports": {
                        "8265/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "8265"}
                        ],
                        "6379/tcp": None,
                    }
                }
            },
            {"NetworkSettings": {"Ports": {"8265/tcp": None}}},
            {"NetworkSettings": {"Ports": {}}},
        ]

        dashboard, others = _published_ports(documents)

        self.assertEqual(
            dashboard,
            [
                {
                    "host_ip": "127.0.0.1",
                    "host_port": 8265,
                    "container_port": 8265,
                    "protocol": "tcp",
                }
            ],
        )
        self.assertEqual(others, [])

    def test_any_other_published_ray_port_is_retained_for_rejection(self) -> None:
        documents = [
            {
                "NetworkSettings": {
                    "Ports": {
                        "8265/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "8265"}
                        ],
                        "6379/tcp": [
                            {"HostIp": "0.0.0.0", "HostPort": "6379"}
                        ],
                    }
                }
            },
            {"NetworkSettings": {"Ports": {}}},
            {"NetworkSettings": {"Ports": {}}},
        ]

        _, others = _published_ports(documents)

        self.assertEqual(others[0]["container_port"], 6379)

    def test_secret_scan_counts_value_without_returning_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean = root / "clean.log"
            leaked = root / "report.json"
            clean.write_bytes(b"safe")
            leaked.write_bytes(b'{"value":"secret-canary"}')

            count, matches, report_match = _scan_files(
                [clean, leaked],
                b"secret-canary",
            )

        self.assertEqual(count, 2)
        self.assertEqual(matches, 1)
        self.assertTrue(report_match)


if __name__ == "__main__":
    unittest.main()
