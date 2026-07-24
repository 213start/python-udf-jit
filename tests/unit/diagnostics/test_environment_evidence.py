from __future__ import annotations

import copy
import unittest

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
    validate_auth_evidence,
    validate_cleanup_evidence,
    validate_secret_evidence,
)


def _auth() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "dashboard": {
                "published_bindings": [
                    {
                        "host_ip": "127.0.0.1",
                        "host_port": 8265,
                        "container_port": 8265,
                        "protocol": "tcp",
                    }
                ],
                "published_non_dashboard_ports": [],
                "non_loopback_connect": "refused",
                "requests": {
                    "unauthenticated": 401,
                    "wrong_token": 403,
                    "authenticated": 200,
                },
                "token_file_mode": "0600",
            },
            "secret_scan": {
                "scanned_artifact_count": 12,
                "scanned_image_count": 1,
                "token_matches": 0,
                "token_in_image_environment": False,
                "token_in_image_history": False,
                "token_in_retained_reports": False,
            },
        }
    )


def _cleanup() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "before": {
                "routes_sha256": "a" * 64,
                "firewall_sha256": "b" * 64,
            },
            "after": {
                "routes_sha256": "a" * 64,
                "firewall_sha256": "b" * 64,
            },
            "cleanup": {
                "removed_container_ids": ["c" * 64, "d" * 64, "e" * 64],
                "removed_network_ids": ["f" * 64, "1" * 64],
                "remaining_project_containers": [],
                "remaining_project_networks": [],
                "dashboard_port_open": False,
                "token_exists": False,
            },
        }
    )


class EnvironmentEvidenceTests(unittest.TestCase):
    def test_exact_auth_and_secret_negative_probes_pass(self) -> None:
        proof = _auth()

        self.assertEqual(
            validate_auth_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )
        self.assertEqual(
            validate_secret_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

    def test_wrong_status_non_loopback_exposure_or_token_leak_fails(self) -> None:
        cases = []
        wrong_status = _auth()
        wrong_status["dashboard"]["requests"]["wrong_token"] = 401
        cases.append((wrong_status, validate_auth_evidence))

        exposed = _auth()
        exposed["dashboard"]["non_loopback_connect"] = "connected"
        cases.append((exposed, validate_auth_evidence))

        leaked = _auth()
        leaked["secret_scan"]["token_matches"] = 1
        cases.append((leaked, validate_secret_evidence))

        for proof, validator in cases:
            with self.subTest(validator=validator.__name__):
                self.assertEqual(
                    validator(
                        proof,
                        run_id="u13-run",
                        cluster_epoch="u13-epoch",
                    ),
                    "fail",
                )

    def test_cleanup_requires_exact_resource_removal_and_host_state_restore(self) -> None:
        proof = _cleanup()
        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

        drift = copy.deepcopy(proof)
        drift["after"]["routes_sha256"] = "9" * 64
        self.assertEqual(
            validate_cleanup_evidence(
                drift,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )

        residue = copy.deepcopy(proof)
        residue["cleanup"]["remaining_project_networks"] = ["left-over"]
        self.assertEqual(
            validate_cleanup_evidence(
                residue,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )

    def test_missing_structured_proof_is_incomplete(self) -> None:
        self.assertEqual(
            validate_auth_evidence(
                {},
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "incomplete",
        )
        self.assertEqual(
            validate_cleanup_evidence(
                None,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "incomplete",
        )


if __name__ == "__main__":
    unittest.main()
