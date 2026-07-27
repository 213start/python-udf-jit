from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.acceptance import (
    AcceptanceContractError,
    evaluate_mainline_prerequisites,
    load_acceptance_contract,
    load_mainline_prerequisite_report,
)


ROOT = Path(__file__).resolve().parents[2]


class MainlineContractIntegrationTests(unittest.TestCase):
    def test_support_matrix_and_acceptance_profile_agree(self) -> None:
        matrix_path = ROOT / "config/mainline-support-matrix.json"
        matrix_bytes = matrix_path.read_bytes()
        matrix = json.loads(matrix_bytes)
        contract = load_acceptance_contract(
            ROOT / "config/mainline-production-acceptance.json"
        )

        self.assertEqual(matrix["profile"], contract.profile)
        self.assertEqual(matrix["execution_contract"]["provider"], "scalar_python")
        self.assertFalse(matrix["execution_contract"]["vector_enabled"])
        self.assertFalse(matrix["execution_contract"]["arrow_enabled"])
        self.assertEqual(
            matrix["advanced_rfc_state"],
            {f"RFC-{i:03d}": "disabled" for i in range(9, 13)},
        )
        self.assertEqual(
            contract.support_matrix_sha256,
            hashlib.sha256(matrix_bytes).hexdigest(),
        )
        report = load_mainline_prerequisite_report(
            ROOT / "config/mainline-production-acceptance.json",
            contract=contract,
        )
        self.assertEqual(
            set(report["gates"].values()),
            {"stop"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mainline-support-matrix.json").write_bytes(
                matrix_bytes + b"\n"
            )
            with self.assertRaisesRegex(
                AcceptanceContractError,
                "support_matrix_hash_mismatch",
            ):
                load_mainline_prerequisite_report(
                    root / "mainline-production-acceptance.json",
                    contract=contract,
                )

    def test_external_platform_contract_separates_admission_and_coverage(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        platform = matrix["external_platform_contract"]

        self.assertEqual(platform["credential_scope"], "job")
        self.assertEqual(platform["trust_domain"], "same_job")
        authentication = platform["artifact_authentication"]
        self.assertEqual(authentication["algorithm"], "hmac-sha256")
        self.assertEqual(authentication["tag_size_bytes"], 32)
        self.assertEqual(
            authentication["key_material_transport"],
            "credential_handle_only",
        )
        self.assertEqual(authentication["downgrade"], "forbidden")
        self.assertEqual(
            set(authentication["covered_fields"]),
            {
                "artifact_schema_version",
                "artifact_content_sha256",
                "manifest_sha256",
                "job_id",
                "tenant_id",
                "key_id",
                "key_generation",
                "issued_at_ns",
                "expires_at_ns",
            },
        )
        self.assertEqual(
            list(platform["admission"]),
            ["commit", "node_join", "task_entry"],
        )
        self.assertEqual(platform["worker_qualification"]["required_workers"], 2)
        self.assertNotEqual(
            platform["worker_qualification"],
            platform["natural_business_coverage"],
        )

    def test_current_support_window_blocks_but_next_baseline_trial_does_not(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        risks = matrix["maintenance_risks"]
        windows = matrix["support_windows"]

        self.assertTrue(risks["current_locked_baseline"]["release_blocking"])
        self.assertTrue(
            windows["current_locked_baseline"][
                "unsupported_or_expired_is_release_blocker"
            ]
        )
        self.assertFalse(windows["next_baseline_trial"]["u2_blocking"])
        self.assertFalse(risks["next_baseline_trial"]["release_blocking"])
        self.assertFalse(risks["next_baseline_trial"]["u2_blocking"])
        self.assertEqual(
            risks["next_baseline_trial"]["disposition"],
            "track_without_blocking_u2",
        )

    def test_unknown_external_prerequisites_are_machine_readable_stops(
        self,
    ) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        prerequisites = matrix["release_prerequisites"]

        support = prerequisites["current_component_support"]
        self.assertEqual(support["status"], "incomplete")
        self.assertEqual(support["gate_outcome"], "stop")
        for field in (
            "delivery_window",
            "first_adoption_window",
            "remaining_support_days",
            "minimum_remaining_support_days",
            "lifecycle_owner",
            "verified_on",
        ):
            self.assertIn(field, support)
        self.assertEqual(
            set(support["component_evidence"]),
            {
                "python",
                "cinderx",
                "daft",
                "ray",
                "lance",
                "pyarrow",
            },
        )
        for component, evidence in support["component_evidence"].items():
            with self.subTest(component=component):
                self.assertIsNone(evidence["support_ends_on"])
                self.assertIsNone(evidence["remaining_support_days"])
                self.assertIsNone(evidence["verified_on"])
                self.assertIsNone(evidence["source"])

        multi_node = prerequisites["multi_node_environment"]
        self.assertEqual(multi_node["status"], "incomplete")
        self.assertEqual(multi_node["gate_outcome"], "stop")
        self.assertIn("owner", multi_node)
        self.assertIn("reservation", multi_node)
        self.assertIn("deadline", multi_node)

        adopter = prerequisites["first_adopter"]
        self.assertEqual(adopter["status"], "incomplete")
        self.assertEqual(adopter["rollout_ceiling"], "observe-ready")
        self.assertFalse(adopter["blocks_functional_completion"])

        report = evaluate_mainline_prerequisites(matrix)
        self.assertEqual(report["unit_completion_status"], "incomplete")
        self.assertEqual(
            report["gates"]["current_component_support"],
            "stop",
        )
        self.assertEqual(
            report["gates"]["multi_node_environment"],
            "stop",
        )
        self.assertIn("current_component_support.lifecycle_owner", report["missing"])

    def test_support_window_pass_requires_consistent_dates_and_remaining_days(
        self,
    ) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        support = matrix["release_prerequisites"][
            "current_component_support"
        ]
        support.update(
            {
                "status": "complete",
                "gate_outcome": "pass",
                "delivery_window": {
                    "starts_on": "2026-08-01",
                    "ends_on": "2026-10-31",
                },
                "first_adoption_window": {
                    "starts_on": "2026-09-01",
                    "ends_on": "2026-12-31",
                },
                "remaining_support_days": 365,
                "minimum_remaining_support_days": 180,
                "lifecycle_owner": "runtime-maintainers",
                "verified_on": "2026-07-27",
            }
        )
        for component, evidence in support["component_evidence"].items():
            evidence.update(
                {
                    "support_ends_on": "2027-07-27",
                    "remaining_support_days": 365,
                    "verified_on": "2026-07-27",
                    "source": f"https://support.example/{component}",
                }
            )

        report = evaluate_mainline_prerequisites(matrix)

        self.assertEqual(
            report["gates"]["current_component_support"],
            "pass",
        )

        invalid = copy.deepcopy(matrix)
        invalid["release_prerequisites"]["current_component_support"][
            "component_evidence"
        ]["python"]["remaining_support_days"] = -1
        with self.assertRaisesRegex(
            AcceptanceContractError,
            "release_prerequisite_false_claim:current_component_support",
        ):
            evaluate_mainline_prerequisites(invalid)

    def test_first_adopter_registration_alone_does_not_authorize_auto(
        self,
    ) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        adopter = matrix["release_prerequisites"]["first_adopter"]
        adopter.update(
            {
                "status": "complete",
                "rollout_ceiling": "observe-ready",
                "business_owner": "named-business-owner",
                "named_job": "representative-daft-job",
                "job_fingerprint": "sha256:" + "a" * 64,
                "target_cluster": "production-cluster",
                "off_baseline_evidence": "evidence://baseline",
                "scalar_matrix_coverage": 0.75,
                "verified_on": "2026-07-27",
            }
        )

        report = evaluate_mainline_prerequisites(matrix)

        self.assertEqual(report["rollout_ceiling"], "observe-ready")
        self.assertEqual(
            report["non_blocking_tracking"]["first_adopter"],
            "complete",
        )

        adopter["rollout_ceiling"] = "adopter-canary-authorized"
        with self.assertRaisesRegex(
            AcceptanceContractError,
            "release_prerequisite_false_claim:first_adopter",
        ):
            evaluate_mainline_prerequisites(matrix)


if __name__ == "__main__":
    unittest.main()
