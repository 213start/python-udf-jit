from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.acceptance import (
    AcceptanceContractError,
    CompletionStatus,
    GateOutcome,
    aggregate_formal_acceptance,
    aggregate_profile_outcomes,
    load_acceptance_contract,
    load_mainline_prerequisite_report,
)


ROOT = Path(__file__).resolve().parents[3]
SCALAR_PATH = ROOT / "config/scalar-piercing-acceptance.json"
MAINLINE_PATH = ROOT / "config/mainline-production-acceptance.json"
MAINLINE_SCHEMA_PATH = (
    ROOT / "config/mainline-release-prerequisites.schema.json"
)


class MainlineAcceptanceContractTests(unittest.TestCase):
    def test_mainline_schema_tracks_release_tier_and_all_examples(self) -> None:
        schema = json.loads(
            MAINLINE_SCHEMA_PATH.read_text(encoding="utf-8")
        )

        self.assertFalse(schema["additionalProperties"])
        examples = schema["properties"]["acceptance_examples"]
        self.assertEqual(examples["minProperties"], 12)
        self.assertEqual(examples["maxProperties"], 12)
        self.assertEqual(
            schema["properties"]["required_gates"]["minItems"],
            57,
        )
        self.assertEqual(
            schema["properties"]["gates"]["minProperties"],
            57,
        )
        self.assertIn(
            "release",
            schema["$defs"]["gate"]["properties"]["tier"]["enum"],
        )
        self.assertIn(
            "release",
            schema["properties"]["test_tier_mapping"][
                "additionalProperties"
            ]["items"]["enum"],
        )

    def test_scalar_and_mainline_profiles_load_independently(self) -> None:
        scalar = load_acceptance_contract(
            SCALAR_PATH,
            expected_profile="u13-formal-scalar-mainline-acceptance",
        )
        mainline = load_acceptance_contract(
            MAINLINE_PATH,
            expected_profile="mainline-production",
        )

        self.assertNotEqual(scalar.schema_id, mainline.schema_id)
        self.assertEqual(set(scalar.requirements), {f"R{i}" for i in range(1, 21)})
        self.assertEqual(
            set(mainline.requirements), {f"R{i}" for i in range(1, 26)}
        )
        self.assertEqual(
            set(mainline.rfc_gates), {f"RFC-{i:03d}" for i in range(1, 9)}
        )
        for index, gates in enumerate(
            mainline.rfc_gates.values(),
            start=1,
        ):
            prefix = f"rfc{index:03d}."
            self.assertEqual(len(gates), 3)
            self.assertTrue(all(gate.startswith(prefix) for gate in gates))
            self.assertEqual(
                {mainline.gates[gate].tier for gate in gates},
                {"unit", "integration", "system"},
            )
        self.assertEqual(
            set(scalar.acceptance_examples),
            {f"AE{i}" for i in range(1, 9)},
        )
        self.assertEqual(
            set(mainline.acceptance_examples),
            {f"AE{i}" for i in range(1, 13)},
        )
        self.assertEqual(
            mainline.disabled_rfcs,
            tuple(f"RFC-{i:03d}" for i in range(9, 13)),
        )
        self.assertEqual(set(mainline.test_suites), {"unit", "integration", "live"})
        self.assertEqual(
            set(mainline.test_tier_mapping), set(mainline.requirements)
        )
        self.assertEqual(
            {
                tier
                for tiers in mainline.test_tier_mapping.values()
                for tier in tiers
            },
            {"unit", "integration", "system", "release"},
        )
        for requirement, gates in mainline.requirements.items():
            with self.subTest(requirement=requirement):
                expected = tuple(
                    tier
                    for tier in ("unit", "integration", "system", "release")
                    if tier
                    in {mainline.gates[gate].tier for gate in gates}
                )
                self.assertEqual(
                    mainline.test_tier_mapping[requirement],
                    expected,
                )

    def test_unknown_profile_schema_duplicate_and_missing_gate_are_rejected(self) -> None:
        document = json.loads(MAINLINE_PATH.read_text(encoding="utf-8"))
        cases = (
            ("profile", "unknown-profile", "contract_profile_unknown"),
            ("contract_schema", "unknown-schema", "contract_schema_unknown"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for field, value, error in cases:
                with self.subTest(field=field):
                    changed = dict(document)
                    changed[field] = value
                    path = root / f"{field}.json"
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(AcceptanceContractError, error):
                        load_acceptance_contract(path)

            missing = json.loads(json.dumps(document))
            gate = missing["required_gates"].pop()
            missing["gates"].pop(gate)
            path = root / "missing.json"
            path.write_text(json.dumps(missing), encoding="utf-8")
            with self.assertRaisesRegex(
                AcceptanceContractError, "gates_invalid"
            ):
                load_acceptance_contract(path)

            extra = json.loads(json.dumps(document))
            extra["unversioned_extension"] = {}
            path = root / "extra.json"
            path.write_text(json.dumps(extra), encoding="utf-8")
            with self.assertRaisesRegex(
                AcceptanceContractError,
                "contract_fields_invalid",
            ):
                load_acceptance_contract(path)

            duplicate = (
                '{"schema_version":2,"profile":"mainline-production",'
                '"profile":"mainline-production","contract_schema":'
                '"urn:python-udf-jit:mainline-release-prerequisites:v1"}'
            )
            path = root / "duplicate.json"
            path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(AcceptanceContractError, "duplicate_key"):
                load_acceptance_contract(path)

    def test_advanced_rfcs_cannot_be_enabled_in_mainline(self) -> None:
        document = json.loads(MAINLINE_PATH.read_text(encoding="utf-8"))
        document["disabled_rfcs"].remove("RFC-009")
        document["rfc_gates"]["RFC-009"] = ["measurement.non_gating"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "enabled-rfc-009.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                AcceptanceContractError, "advanced_rfcs_must_be_disabled"
            ):
                load_acceptance_contract(path)

    def test_unit_lifecycle_is_not_a_gate_outcome(self) -> None:
        contract = load_acceptance_contract(MAINLINE_PATH)
        outcomes = {gate: GateOutcome.PASS for gate in contract.gates}

        incomplete = aggregate_profile_outcomes(
            contract,
            outcomes,
            unit_completion_status=CompletionStatus.INCOMPLETE,
        )
        complete = aggregate_profile_outcomes(
            contract,
            outcomes,
            unit_completion_status=CompletionStatus.COMPLETE,
        )

        self.assertEqual(incomplete["verdict"], "pass")
        self.assertFalse(incomplete["release_ready"])
        self.assertEqual(incomplete["unit_completion_status"], "incomplete")
        self.assertNotIn("incomplete", incomplete["gates"].values())
        self.assertEqual(complete["verdict"], "pass")
        self.assertTrue(complete["release_ready"])

    def test_missing_gate_evidence_is_absent_not_inconclusive(self) -> None:
        contract = load_acceptance_contract(MAINLINE_PATH)
        executed_gate = next(iter(contract.gates))

        report = aggregate_profile_outcomes(
            contract,
            {executed_gate: GateOutcome.PASS},
            unit_completion_status=CompletionStatus.COMPLETE,
        )

        self.assertEqual(report["gates"], {executed_gate: "pass"})
        self.assertNotIn("inconclusive", report["gates"].values())
        self.assertEqual(
            set(report["missing_gates"]),
            set(contract.gates) - {executed_gate},
        )
        self.assertIsNone(report["verdict"])
        self.assertEqual(report["executed_gate_verdict"], "pass")
        self.assertEqual(report["unit_completion_status"], "incomplete")
        self.assertFalse(report["release_ready"])

    def test_real_aggregation_only_claims_implemented_rfc_gates(
        self,
    ) -> None:
        from tests.unit.diagnostics.test_acceptance import (
            _evidence,
            _test_proof,
        )

        contract = load_acceptance_contract(MAINLINE_PATH)
        evidence = _evidence()
        for name, suite in contract.test_suites.items():
            evidence["infrastructure"]["python_test_proofs"][name] = _test_proof(
                gate_id=suite.gate_id,
                tier=suite.tier,
                required_tests=list(suite.required_tests),
                test_count=suite.expected_test_count,
            )
        evidence["unit_completion_status"] = "complete"

        report = aggregate_formal_acceptance(contract, evidence)

        self.assertIsNone(report["verdict"])
        self.assertEqual(report["executed_gate_verdict"], "pass")
        self.assertEqual(report["unit_completion_status"], "incomplete")
        self.assertFalse(report["release_ready"])
        self.assertNotIn("incomplete", report["gates"].values())
        self.assertEqual(
            set(report["missing_rfcs"]),
            {f"RFC-{index:03d}" for index in range(4, 9)},
        )
        self.assertEqual(report["rfcs"]["RFC-001"], "pass")
        self.assertEqual(report["rfcs"]["RFC-002"], "pass")
        self.assertEqual(report["rfcs"]["RFC-003"], "pass")
        for rfc in ("RFC-001", "RFC-002", "RFC-003"):
            self.assertEqual(
                {
                    report["gates"][gate]
                    for gate in contract.rfc_gates[rfc]
                },
                {"pass"},
            )
        self.assertTrue(
            all(
                gate.startswith(("rfc", "prerequisite."))
                for gate in report["missing_gates"]
            )
        )

        evidence["mainline_prerequisites"] = (
            load_mainline_prerequisite_report(
                MAINLINE_PATH,
                contract=contract,
            )
        )
        milestone = aggregate_formal_acceptance(contract, evidence)
        self.assertEqual(milestone["executed_gate_verdict"], "pass")
        self.assertNotIn(
            "prerequisite.current_component_support",
            contract.gates,
        )
        self.assertNotIn(
            "prerequisite.credential_distribution",
            contract.gates,
        )
        self.assertNotIn(
            "prerequisite.emergency_distribution",
            contract.gates,
        )
        self.assertIn(
            "prerequisite.multi_node_environment",
            milestone["missing_gates"],
        )

    def test_real_aggregation_records_unexecuted_receipt_as_missing(self) -> None:
        from tests.unit.diagnostics.test_acceptance import _evidence

        contract = load_acceptance_contract(MAINLINE_PATH)
        evidence = _evidence()
        evidence["infrastructure"]["python_test_proofs"].pop("unit")
        evidence["unit_completion_status"] = "complete"

        report = aggregate_formal_acceptance(contract, evidence)

        self.assertNotIn("tests.unit_suite", report["gates"])
        self.assertIn("tests.unit_suite", report["missing_gates"])
        self.assertNotEqual(
            report["gates"].get("tests.unit_suite"),
            "inconclusive",
        )
        self.assertEqual(report["unit_completion_status"], "incomplete")
        self.assertFalse(report["release_ready"])

    def test_directional_baseline_and_formal_qualification_are_separate(self) -> None:
        document = json.loads(MAINLINE_PATH.read_text(encoding="utf-8"))
        baseline = document["performance_baseline"]
        formal = document["formal_performance_qualification"]

        self.assertEqual(baseline["mode"], "directional")
        self.assertEqual(baseline["baseline"]["mode"], "off")
        self.assertEqual(baseline["candidate"]["mode"], "auto")
        self.assertEqual(baseline["environment_constraint"], "same_environment")
        self.assertEqual(baseline["off_runs"], 1)
        self.assertEqual(baseline["auto_runs"], 1)
        self.assertEqual(baseline["conclusion_scope"], "directional_only")
        self.assertNotIn("alternating_measured_runs", baseline)
        self.assertFalse(baseline["blocks_functional_completion"])
        self.assertEqual(formal["cli_flag"], "--formal")
        self.assertEqual(formal["alternating_measured_runs"], 5)
        self.assertEqual(formal["statistic"], "median")
        self.assertEqual(formal["target_speedup"], 1.15)
        self.assertFalse(formal["blocks_functional_completion"])


if __name__ == "__main__":
    unittest.main()
