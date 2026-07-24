from __future__ import annotations

import unittest

from python_udf_jit.diagnostics.environment_evidence import (
    validate_cleanup_evidence,
)
from tests.system.capture_host_state import _canonical_routes
from tests.system.verify_cleanup import build_cleanup_proof


class CleanupProbeTests(unittest.TestCase):
    def test_route_hash_input_is_canonical_json(self) -> None:
        left = _canonical_routes(
            b'[{"dst":"default","gateway":"1.2.3.4","metric":100}]'
        )
        right = _canonical_routes(
            b'[{"metric":100,"gateway":"1.2.3.4","dst":"default"}]'
        )
        self.assertEqual(left, right)

    def test_real_resource_identifiers_and_equal_host_state_seal_proof(self) -> None:
        state = {
            "routes_sha256": "a" * 64,
            "firewall_sha256": "b" * 64,
        }
        proof = build_cleanup_proof(
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            before=state,
            after=state,
            removed_container_ids=["c" * 64, "d" * 64, "e" * 64],
            removed_network_ids=["f" * 64, "1" * 64],
            remaining_project_containers=[],
            remaining_project_networks=[],
            dashboard_port_open=False,
            token_exists=False,
        )

        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

    def test_any_remaining_resource_is_preserved_as_failure_evidence(self) -> None:
        state = {
            "routes_sha256": "a" * 64,
            "firewall_sha256": "b" * 64,
        }
        proof = build_cleanup_proof(
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            before=state,
            after=state,
            removed_container_ids=["c" * 64, "d" * 64, "e" * 64],
            removed_network_ids=["f" * 64, "1" * 64],
            remaining_project_containers=["still-running"],
            remaining_project_networks=[],
            dashboard_port_open=False,
            token_exists=False,
        )

        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
