from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.test_evidence import (
    TestEvidenceError,
    build_unittest_evidence,
    validate_unittest_evidence,
)


class TestEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "unit.log"
        self.path.write_text(
            "test_alpha (tests.Alpha.test_alpha) ... live worker log\n"
            "remote scheduler detail\n"
            "ok\n"
            "test_beta (tests.Beta.test_beta)\n"
            "A documented test whose status is on another line. ... ok\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.125s\n"
            "\n"
            "OK\n",
            encoding="utf-8",
        )
        os.chmod(self.path, 0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self) -> dict[str, object]:
        return build_unittest_evidence(
            gate_id="python.unit",
            tier="unit",
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            source_git_commit="a" * 40,
            argv=(
                "docker",
                "exec",
                "-e",
                "UDFJIT_MODE=auto",
                "-e",
                "UDFJIT_RUN_ID=u13-run",
                "head",
                "python",
                "-m",
                "unittest",
            ),
            required_tests=("test_alpha", "test_beta"),
            minimum_test_count=2,
            allow_skips=False,
            log_path=self.path,
        )

    def test_passing_private_log_produces_identity_bound_receipt(self) -> None:
        proof = self._build()

        self.assertEqual(proof["test_count"], 2)
        self.assertEqual(proof["skipped"], 0)
        self.assertEqual(
            validate_unittest_evidence(
                proof,
                gate_id="python.unit",
                tier="unit",
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                source_git_commit="a" * 40,
                required_tests=("test_alpha", "test_beta"),
                minimum_test_count=2,
                allow_skips=False,
            ),
            "pass",
        )

    def test_missing_required_test_failure_or_skip_is_rejected(self) -> None:
        with self.assertRaisesRegex(TestEvidenceError, "required_test_missing"):
            build_unittest_evidence(
                gate_id="python.unit",
                tier="unit",
                run_id="run",
                cluster_epoch="epoch",
                source_git_commit="a" * 40,
                argv=("python", "-m", "unittest"),
                required_tests=("test_gamma",),
                minimum_test_count=1,
                allow_skips=False,
                log_path=self.path,
            )

        self.path.write_text(
            "test_alpha (tests.Alpha.test_alpha) ... skipped 'missing env'\n"
            "Ran 1 test in 0.01s\n\nOK (skipped=1)\n",
            encoding="utf-8",
        )
        os.chmod(self.path, 0o600)
        with self.assertRaisesRegex(TestEvidenceError, "unexpected_skip"):
            build_unittest_evidence(
                gate_id="python.unit",
                tier="unit",
                run_id="run",
                cluster_epoch="epoch",
                source_git_commit="a" * 40,
                argv=("python", "-m", "unittest"),
                required_tests=("test_alpha",),
                minimum_test_count=1,
                allow_skips=False,
                log_path=self.path,
            )

    def test_receipt_identity_or_command_tampering_fails_validation(self) -> None:
        proof = self._build()
        proof["cluster_epoch"] = "different"
        proof["argv"].append("--unexpected")

        self.assertEqual(
            validate_unittest_evidence(
                proof,
                gate_id="python.unit",
                tier="unit",
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                source_git_commit="a" * 40,
                required_tests=("test_alpha", "test_beta"),
                minimum_test_count=2,
                allow_skips=False,
            ),
            "fail",
        )

    def test_log_must_be_mode_0600(self) -> None:
        os.chmod(self.path, 0o644)

        with self.assertRaisesRegex(TestEvidenceError, "test_log_mode_invalid"):
            self._build()


if __name__ == "__main__":
    unittest.main()
