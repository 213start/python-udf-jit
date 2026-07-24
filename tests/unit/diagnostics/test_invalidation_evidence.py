from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.invalidation_evidence import (
    InvalidationEvidenceError,
    build_invalidation_evidence,
    validate_invalidation_evidence,
)


def _snapshot(worker_boot: str) -> dict[str, object]:
    return {
        "phase": "e2e",
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "manifest_sha256": "a" * 64,
        "image_digest": f"sha256:{'b' * 64}",
        "nodes": [
            {
                "role": "ray-head-driver",
                "node_id": "head",
                "container_boot_id": "head-boot",
            },
            {
                "role": "ray-worker-1",
                "node_id": "worker-1",
                "container_boot_id": "worker-1-boot",
            },
            {
                "role": "ray-worker-2",
                "node_id": "worker-2",
                "container_boot_id": worker_boot,
            },
        ],
    }


class InvalidationEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.before = self.root / "before.json"
        self.after = self.root / "after.json"
        self.report = self.root / "invalidated.json"
        self._write(self.before, _snapshot("worker-2-before"))
        self._write(self.after, _snapshot("worker-2-after"))
        self._write(
            self.report,
            {
                "run_id": "u13-run",
                "cluster_epoch": "u13-epoch",
                "verdict": "inconclusive",
                "reason_codes": ["phase_identity_drift"],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, document: dict[str, object]) -> None:
        path.write_text(
            json.dumps(document, sort_keys=True),
            encoding="ascii",
        )
        os.chmod(path, 0o600)

    def _build(self) -> dict[str, object]:
        return build_invalidation_evidence(
            source_git_commit="c" * 40,
            before_snapshot_path=self.before,
            after_snapshot_path=self.after,
            invalidated_report_path=self.report,
        )

    def test_real_worker_restart_and_inconclusive_report_produce_proof(self) -> None:
        proof = self._build()

        self.assertEqual(proof["probe"]["changed_role"], "ray-worker-2")
        self.assertEqual(
            validate_invalidation_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                source_git_commit="c" * 40,
            ),
            "pass",
        )

    def test_no_restart_or_wrong_verdict_is_rejected(self) -> None:
        self._write(self.after, _snapshot("worker-2-before"))
        with self.assertRaisesRegex(
            InvalidationEvidenceError, "worker_restart_not_observed"
        ):
            self._build()

        self._write(self.after, _snapshot("worker-2-after"))
        self._write(
            self.report,
            {
                "run_id": "u13-run",
                "cluster_epoch": "u13-epoch",
                "verdict": "pass",
                "reason_codes": [],
            },
        )
        with self.assertRaisesRegex(
            InvalidationEvidenceError, "invalidation_verdict_invalid"
        ):
            self._build()

    def test_head_or_other_worker_drift_is_rejected(self) -> None:
        after = _snapshot("worker-2-after")
        after["nodes"][0]["container_boot_id"] = "changed-head"
        self._write(self.after, after)

        with self.assertRaisesRegex(
            InvalidationEvidenceError, "unexpected_stable_role_drift"
        ):
            self._build()


if __name__ == "__main__":
    unittest.main()
