from __future__ import annotations

import unittest

from tests.system.assemble_infrastructure_evidence import assemble


def _proof() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "pass",
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
    }


class AssembleInfrastructureEvidenceTests(unittest.TestCase):
    def test_only_identity_bound_structured_proofs_are_assembled(self) -> None:
        proof = _proof()
        document = assemble(
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            cinderx={"schema_version": 1, "status": "pass"},
            unit=proof,
            integration=proof,
            live=proof,
            auth=proof,
            invalidation=proof,
            cleanup=proof,
            hygiene=proof,
        )

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            set(document["python_test_proofs"]),
            {"unit", "integration", "live"},
        )
        self.assertNotIn("python_unit_tests", document)
        self.assertNotIn("containers_removed", document)

    def test_any_run_or_epoch_mismatch_is_rejected(self) -> None:
        proof = _proof()
        mismatch = {**proof, "cluster_epoch": "other"}

        with self.assertRaisesRegex(ValueError, "cleanup proof Run/Epoch"):
            assemble(
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                cinderx={"schema_version": 1, "status": "pass"},
                unit=proof,
                integration=proof,
                live=proof,
                auth=proof,
                invalidation=proof,
                cleanup=mismatch,
                hygiene=proof,
            )


if __name__ == "__main__":
    unittest.main()
