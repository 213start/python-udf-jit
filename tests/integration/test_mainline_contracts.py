from __future__ import annotations

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
        self.assertEqual(report["unit_completion_status"], "complete")
        self.assertEqual(
            report["gates"],
            {"multi_node_environment": "stop"},
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

    def test_runtime_baselines_separate_target_from_current_validation(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        baselines = matrix["runtime_baselines"]

        self.assertEqual(
            baselines["production_target"]["python"],
            "3.11.6",
        )
        self.assertEqual(
            baselines["production_target"]["cinderx_status"],
            "python_3_11_adaptation_pending",
        )
        self.assertEqual(
            baselines["current_development_validation"]["python"],
            "3.14.3",
        )
        self.assertEqual(
            baselines["current_development_validation"]["purpose"],
            "development_test_and_blue98_validation",
        )
        self.assertFalse(
            baselines["production_target"][
                "blocks_current_functional_development"
            ]
        )

    def test_trusted_job_contract_uses_hash_and_worker_revalidation(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        platform = matrix["external_platform_contract"]

        self.assertEqual(platform["trust_domain"], "same_trusted_ray_job")
        integrity = platform["artifact_integrity"]
        self.assertEqual(integrity["content_hash"], "sha256")
        self.assertEqual(
            integrity["source_authentication"],
            "not_required_in_current_scope",
        )
        self.assertFalse(integrity["external_artifact_ingress"])
        self.assertFalse(integrity["cross_job_artifact_cache"])
        self.assertTrue(integrity["worker_revalidation_required"])
        self.assertNotIn("artifact_authentication", platform)
        self.assertNotIn("credential_scope", platform)
        self.assertEqual(
            list(platform["admission"]),
            ["commit", "node_join", "task_entry"],
        )
        self.assertEqual(platform["worker_qualification"]["required_workers"], 2)
        self.assertNotEqual(
            platform["worker_qualification"],
            platform["natural_business_coverage"],
        )

    def test_only_physical_multinode_is_an_external_release_prerequisite(
        self,
    ) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        prerequisites = matrix["release_prerequisites"]

        self.assertEqual(
            set(prerequisites),
            {
                "target_runtime_adaptation",
                "multi_node_environment",
                "first_adopter",
            },
        )

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
        self.assertEqual(report["unit_completion_status"], "complete")
        self.assertEqual(
            report["gates"],
            {"multi_node_environment": "stop"},
        )
        self.assertEqual(
            report["future_blocking"],
            {"multi_node_environment": "stop"},
        )
        self.assertFalse(
            prerequisites["target_runtime_adaptation"][
                "blocks_current_functional_work"
            ]
        )

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
