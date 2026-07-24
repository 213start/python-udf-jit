from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("UDFJIT_FORMAL_ACCEPTANCE") == "1",
    "requires a completed blue-98 U13 formal acceptance run",
)
class LiveFormalAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_path = os.environ.get("UDFJIT_FORMAL_ACCEPTANCE_REPORT_PATH", "")
        if not raw_path:
            raise AssertionError(
                "UDFJIT_FORMAL_ACCEPTANCE_REPORT_PATH is required"
            )
        cls.path = Path(raw_path)
        cls.report = json.loads(cls.path.read_text(encoding="ascii"))

    def test_source_run_and_report_identity_are_locked(self) -> None:
        self.assertEqual(
            stat.S_IMODE(self.path.stat().st_mode),
            0o600,
        )
        self.assertEqual(self.report["run_id"], os.environ["UDFJIT_RUN_ID"])
        self.assertEqual(
            self.report["cluster_epoch"],
            os.environ["UDFJIT_CLUSTER_EPOCH"],
        )
        self.assertEqual(
            self.report["source"]["git_commit"],
            os.environ["UDFJIT_GIT_COMMIT"],
        )

    def test_all_ut_it_and_st_gates_pass(self) -> None:
        self.assertEqual(
            self.report["verdict"],
            "pass",
            self.report.get("reason_codes"),
        )
        for gate, status in self.report["gates"].items():
            with self.subTest(gate=gate):
                self.assertEqual(status, "pass")

    def test_every_requirement_and_acceptance_example_passes(self) -> None:
        for collection in ("requirements", "acceptance_examples"):
            for identifier, status in self.report[collection].items():
                with self.subTest(collection=collection, identifier=identifier):
                    self.assertEqual(status, "pass")


if __name__ == "__main__":
    unittest.main()
