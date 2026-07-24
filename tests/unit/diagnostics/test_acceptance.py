from __future__ import annotations

import copy
import importlib
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.acceptance import (
    AcceptanceContractError,
    aggregate_formal_acceptance,
    load_acceptance_contract,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "config/scalar-piercing-acceptance.json"
HEX_64 = "a" * 64
IMAGE_DIGEST = f"sha256:{'b' * 64}"


def _base_report() -> dict[str, object]:
    return {
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "verdict": "pass",
        "reason_codes": [],
        "checks": {
            "manifest": "pass",
            "evidence_identity": "pass",
            "readiness": "pass",
            "worker_pool_qualification": "pass",
            "supported_hit": "pass",
            "guard_miss": "pass",
            "unsupported": "pass",
            "fail_open": "pass",
            "zero_row": "pass",
            "data_plane_isolation": "pass",
            "attempt_attribution": "pass",
        },
        "manifest": {
            "image_digest": IMAGE_DIGEST,
            "udf_jit_wheel_sha256": HEX_64,
        },
    }


def _scenario(
    *, row_count: int, ordered_digest: str = HEX_64, callable_calls: int = 0
) -> dict[str, object]:
    return {
        "completed": True,
        "ordered_result_sha256": ordered_digest,
        "schema_sha256": "c" * 64,
        "row_count": row_count,
        "callable_calls": callable_calls,
        "side_effect_count": callable_calls,
    }


def _black_box(mode: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "mode": mode,
        "user_script_sha256": "d" * 64,
        "plugin_import_count": 0,
        "bootstrap_hooks_installed": mode == "auto",
        "scenarios": {
            "with_column": _scenario(row_count=4),
            "with_columns": _scenario(row_count=4),
            "unsupported": _scenario(row_count=4, callable_calls=4),
            "exception": {
                "completed": True,
                "exception_type": "daft.exceptions.DaftCoreException",
                "user_exception_type_observed": True,
                "message_sentinel_observed": True,
                "callable_calls": 1,
                "side_effect_count": 1,
            },
            "zero_row": _scenario(row_count=0),
        },
    }


def _evidence() -> dict[str, object]:
    return {
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "source": {
            "git_commit": "e" * 40,
            "dirty": False,
            "image_digest": IMAGE_DIGEST,
            "udf_jit_wheel_sha256": HEX_64,
        },
        "base_report": _base_report(),
        "black_box": {
            "off": _black_box("off"),
            "auto": _black_box("auto"),
        },
        "infrastructure": {
            "object_store_data_plane": True,
            "cinderx_runtime_tests": True,
            "cinderx_python_tests": True,
            "dashboard_unauthenticated_status": 401,
            "dashboard_wrong_token_status": 403,
            "dashboard_authenticated_status": 200,
            "dashboard_loopback_only": True,
            "other_ray_ports_unpublished": True,
            "secret_hygiene": True,
            "raw_events_removed": True,
            "report_permissions_0600": True,
            "python_unit_tests": True,
            "python_integration_tests": True,
            "live_tests_executed": True,
            "containers_removed": True,
            "networks_removed": True,
            "dashboard_port_closed": True,
            "firewall_restored": True,
            "routes_restored": True,
            "token_removed": True,
        },
        "measurement": {
            "completed": True,
            "semantic_equivalent": True,
            "speedup_gate_applied": False,
        },
    }


class AcceptanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_acceptance_contract(CONTRACT_PATH)

    def test_contract_traces_every_requirement_and_acceptance_example(self) -> None:
        self.assertEqual(
            set(self.contract.requirements),
            {f"R{index}" for index in range(1, 21)},
        )
        self.assertEqual(
            set(self.contract.acceptance_examples),
            {f"AE{index}" for index in range(1, 9)},
        )
        for identifier, gates in (
            *self.contract.requirements.items(),
            *self.contract.acceptance_examples.items(),
        ):
            with self.subTest(identifier=identifier):
                self.assertTrue(gates)
                self.assertTrue(set(gates) <= set(self.contract.gates))

        system_requirements = {"R2", "R3", "R9", "R10", "R11", "R14", "R17", "R18", "R20"}
        for identifier in system_requirements:
            with self.subTest(identifier=identifier):
                tiers = {
                    self.contract.gates[gate].tier
                    for gate in self.contract.requirements[identifier]
                }
                self.assertIn("system", tiers)
        for identifier, gates in self.contract.acceptance_examples.items():
            with self.subTest(identifier=identifier):
                self.assertIn(
                    "system",
                    {self.contract.gates[gate].tier for gate in gates},
                )

    def test_every_gate_references_an_existing_test_method(self) -> None:
        for gate_id, gate in self.contract.gates.items():
            for target in gate.test_targets:
                with self.subTest(gate=gate_id, target=target):
                    module_name, class_name, method_name = target.rsplit(".", 2)
                    module = importlib.import_module(module_name)
                    test_class = getattr(module, class_name)
                    self.assertTrue(issubclass(test_class, unittest.TestCase))
                    self.assertTrue(callable(getattr(test_class, method_name)))

    def test_all_required_gates_produce_a_pass_report(self) -> None:
        report = aggregate_formal_acceptance(self.contract, _evidence())

        self.assertEqual(report["verdict"], "pass", report["reason_codes"])
        self.assertEqual(set(report["requirements"]), set(self.contract.requirements))
        self.assertEqual(
            set(report["acceptance_examples"]),
            set(self.contract.acceptance_examples),
        )
        self.assertTrue(
            all(status == "pass" for status in report["gates"].values())
        )

    def test_missing_or_false_external_proof_cannot_silently_pass(self) -> None:
        for field in (
            "object_store_data_plane",
            "cinderx_runtime_tests",
            "dashboard_loopback_only",
            "secret_hygiene",
            "containers_removed",
            "routes_restored",
        ):
            with self.subTest(field=field):
                evidence = _evidence()
                evidence["infrastructure"][field] = False
                report = aggregate_formal_acceptance(self.contract, evidence)
                self.assertEqual(report["verdict"], "fail")
                self.assertTrue(report["reason_codes"])

        evidence = _evidence()
        del evidence["infrastructure"]["routes_restored"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn("gate_missing:environment.cleanup", report["reason_codes"])

    def test_black_box_drift_or_test_only_import_fails_transparency(self) -> None:
        cases = []

        imported = _evidence()
        imported["black_box"]["auto"]["plugin_import_count"] = 1
        cases.append(imported)

        reordered = _evidence()
        reordered["black_box"]["auto"]["scenarios"]["with_column"][
            "ordered_result_sha256"
        ] = "f" * 64
        cases.append(reordered)

        exception_drift = _evidence()
        exception_drift["black_box"]["auto"]["scenarios"]["exception"][
            "exception_type"
        ] = "builtins.RuntimeError"
        cases.append(exception_drift)

        for evidence in cases:
            with self.subTest():
                report = aggregate_formal_acceptance(self.contract, evidence)
                self.assertEqual(report["verdict"], "fail")

    def test_base_inconclusive_and_dirty_source_are_preserved(self) -> None:
        evidence = _evidence()
        evidence["base_report"]["verdict"] = "inconclusive"
        evidence["base_report"]["checks"]["attempt_attribution"] = "inconclusive"
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "inconclusive")

        evidence = _evidence()
        evidence["source"]["dirty"] = True
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn("gate_failed:provenance.clean_source", report["reason_codes"])

    def test_unknown_evidence_shape_is_rejected(self) -> None:
        evidence = copy.deepcopy(_evidence())
        evidence["black_box"]["off"]["mode"] = "observe"

        with self.assertRaises(AcceptanceContractError):
            aggregate_formal_acceptance(self.contract, evidence)


if __name__ == "__main__":
    unittest.main()
