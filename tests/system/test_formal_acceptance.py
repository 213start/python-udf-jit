from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path


def assert_accepted_gate_scope(report: dict[str, object]) -> None:
    release_ready = report.get("release_ready") is True
    if release_ready:
        if report.get("verdict") != "pass":
            raise AssertionError(report.get("reason_codes"))
        if report.get("missing_gates"):
            raise AssertionError("release-ready report has missing gates")
    else:
        if report.get("unit_completion_status") != "incomplete":
            raise AssertionError("milestone report must remain incomplete")
        if report.get("verdict") is not None:
            raise AssertionError("milestone report cannot claim a final verdict")
        if report.get("executed_gate_verdict") != "pass":
            raise AssertionError(report.get("reason_codes"))
        if not report.get("missing_gates"):
            raise AssertionError("milestone report must name future gates")
    for gate, status in dict(report["gates"]).items():
        if status != "pass":
            raise AssertionError(f"{gate}:{status}")


def assert_accepted_mapping_scope(
    report: dict[str, object],
    collection: str,
) -> None:
    executed = dict(report[collection])
    missing = dict(report[f"missing_{collection}"])
    overlap = set(executed) & set(missing)
    if overlap:
        raise AssertionError(f"{collection}_scope_overlap:{sorted(overlap)}")
    for identifier, status in executed.items():
        if status != "pass":
            raise AssertionError(f"{collection}:{identifier}:{status}")
    if report.get("release_ready") is True and missing:
        raise AssertionError(f"release-ready report has missing {collection}")


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
        assert_accepted_gate_scope(self.report)

    def test_every_requirement_and_acceptance_example_passes(self) -> None:
        for collection in ("requirements", "acceptance_examples"):
            assert_accepted_mapping_scope(self.report, collection)


if __name__ == "__main__":
    unittest.main()
