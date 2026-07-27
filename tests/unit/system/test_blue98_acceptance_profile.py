from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.test_evidence import (
    TestEvidenceError,
    validate_unittest_evidence,
)
from python_udf_jit.diagnostics.acceptance import TestSuiteContract
from tests.system.run_blue98_acceptance import (
    AcceptanceRunError,
    _acceptance_report_passes,
    _resolve_acceptance_profile,
    _test_receipt,
)


class Blue98AcceptanceProfileTests(unittest.TestCase):
    def test_milestone_run_does_not_claim_or_require_release_readiness(
        self,
    ) -> None:
        report = {
            "verdict": None,
            "executed_gate_verdict": "pass",
            "release_ready": False,
            "unit_completion_status": "incomplete",
        }

        self.assertTrue(
            _acceptance_report_passes(
                report,
                require_release_ready=False,
            )
        )
        self.assertFalse(
            _acceptance_report_passes(
                report,
                require_release_ready=True,
            )
        )
        failed = {**report, "executed_gate_verdict": "fail"}
        self.assertFalse(
            _acceptance_report_passes(
                failed,
                require_release_ready=False,
            )
        )

    def test_named_profiles_and_explicit_json_paths_resolve(self) -> None:
        repository = Path(__file__).resolve().parents[3]

        self.assertEqual(
            _resolve_acceptance_profile(repository, "scalar-piercing"),
            repository / "config/scalar-piercing-acceptance.json",
        )
        self.assertEqual(
            _resolve_acceptance_profile(repository, "mainline-production"),
            repository / "config/mainline-production-acceptance.json",
        )
        explicit = repository / "config/mainline-production-acceptance.json"
        self.assertEqual(
            _resolve_acceptance_profile(repository, str(explicit)),
            explicit,
        )
        with self.assertRaisesRegex(
            AcceptanceRunError,
            "acceptance_profile_unknown",
        ):
            _resolve_acceptance_profile(repository, "future-profile")

    def test_receipt_rejects_exact_count_drift_from_profile(self) -> None:
        suite = TestSuiteContract(
            gate_id="python.unit",
            tier="unit",
            arguments=("discover", "-s", "tests/unit", "-v"),
            required_tests=("test_required",),
            expected_test_count=2,
            allow_skips=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "unit.log"
            log.write_text(
                "test_required (tests.Required.test_required) ... ok\n"
                "test_extra_a (tests.Extra.test_extra_a) ... ok\n"
                "test_extra_b (tests.Extra.test_extra_b) ... ok\n"
                "\nRan 3 tests in 0.001s\n\nOK\n",
                encoding="utf-8",
            )
            os.chmod(log, 0o600)
            output = root / "proof.json"

            with self.assertRaisesRegex(
                (AcceptanceRunError, TestEvidenceError),
                "test_count|unittest_result_invalid",
            ):
                _test_receipt(
                    suite=suite,
                    run_id="run",
                    cluster_epoch="epoch",
                    git_commit="a" * 40,
                    argv=["python", "-m", "unittest"],
                    log_path=log,
                    output=output,
                )

    def test_receipt_validation_rejects_configured_count_loss(self) -> None:
        suite = TestSuiteContract(
            gate_id="python.unit",
            tier="unit",
            arguments=("tests.unit.example", "-v"),
            required_tests=("test_required",),
            expected_test_count=1,
            allow_skips=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "unit.log"
            log.write_text(
                "test_required (tests.Required.test_required) ... ok\n"
                "\nRan 1 test in 0.001s\n\nOK\n",
                encoding="utf-8",
            )
            os.chmod(log, 0o600)
            proof = _test_receipt(
                suite=suite,
                run_id="run",
                cluster_epoch="epoch",
                git_commit="a" * 40,
                argv=["python", "-m", "unittest"],
                log_path=log,
                output=root / "proof.json",
            )

        self.assertEqual(
            validate_unittest_evidence(
                proof,
                gate_id=suite.gate_id,
                tier=suite.tier,
                run_id="run",
                cluster_epoch="epoch",
                source_git_commit="a" * 40,
                required_tests=suite.required_tests,
                minimum_test_count=2,
                expected_test_count=2,
                allow_skips=suite.allow_skips,
            ),
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
