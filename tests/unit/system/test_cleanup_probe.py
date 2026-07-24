from __future__ import annotations

import copy
import unittest

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
    validate_cleanup_evidence,
)
from tests.system.capture_host_state import _canonical_routes
from tests.system.run_blue98_acceptance import _project_ids
from tests.system.verify_cleanup import build_cleanup_proof


def _state() -> dict[str, object]:
    return {
        "routes_sha256": "a" * 64,
        "firewall_sha256": "b" * 64,
        "firewalld_runtime_sha256": "c" * 64,
        "firewalld_permanent_sha256": "d" * 64,
        "firewall_backend": "nftables-stateless",
        "firewalld_state": "running",
    }


def _bridge() -> dict[str, object]:
    network_id = "f" * 64
    return {
        "action": "runtime-trusted",
        "network_id": network_id,
        "bridge_interface": f"br-{network_id[:12]}",
        "zone": "trusted",
        "scope": "runtime",
        "connectivity_before": {
            "ray-worker-1": False,
            "ray-worker-2": False,
        },
        "connectivity_after": {
            "ray-worker-1": True,
            "ray-worker-2": True,
        },
        "binding_added": True,
        "binding_removed": True,
        "bridge_interface_exists_after_cleanup": False,
    }


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
        class ProjectCommands:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, arguments: list[str]) -> str:
                self.calls.append(arguments)
                if arguments[1] == "ps":
                    return f"{'c' * 64}\n{'d' * 64}\n{'e' * 64}\n"
                return f"{'f' * 64}\n{'1' * 64}\n"

        commands = ProjectCommands()
        self.assertEqual(
            _project_ids(commands, kind="container", project="u13-project"),
            ["c" * 64, "d" * 64, "e" * 64],
        )
        self.assertEqual(
            _project_ids(commands, kind="network", project="u13-project"),
            ["1" * 64, "f" * 64],
        )
        self.assertTrue(
            all("--no-trunc" in arguments for arguments in commands.calls)
        )

        state = _state()
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
            bridge_accommodation=_bridge(),
        )

        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

        short_document = copy.deepcopy(
            {
                key: value
                for key, value in proof.items()
                if key != "proof_sha256"
            }
        )
        short_document["cleanup"]["removed_network_ids"] = [
            identifier[:12]
            for identifier in proof["cleanup"]["removed_network_ids"]
        ]
        short_ids = seal_environment_proof(short_document)
        self.assertEqual(
            validate_cleanup_evidence(
                short_ids,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )

    def test_any_remaining_resource_is_preserved_as_failure_evidence(self) -> None:
        state = _state()
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
            bridge_accommodation=_bridge(),
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
