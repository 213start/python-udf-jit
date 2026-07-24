from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.environment_evidence import (
    validate_hygiene_evidence,
)
from tests.system.build_hygiene_evidence import build_hygiene_proof


class HygieneEvidenceTests(unittest.TestCase):
    def test_private_retained_report_and_removed_raw_event_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "base-report.json"
            report.write_text("{}", encoding="ascii")
            os.chmod(report, 0o600)
            raw = root / "events.jsonl"
            proof = build_hygiene_proof(
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                retained_reports=[report],
                raw_event_files=[raw],
            )

        self.assertEqual(
            validate_hygiene_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

    def test_public_report_or_remaining_raw_event_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "base-report.json"
            report.write_text("{}", encoding="ascii")
            os.chmod(report, 0o644)
            raw = root / "events.jsonl"
            raw.write_text("{}\n", encoding="ascii")
            proof = build_hygiene_proof(
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                retained_reports=[report],
                raw_event_files=[raw],
            )

        self.assertEqual(
            validate_hygiene_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
