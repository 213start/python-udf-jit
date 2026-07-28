from __future__ import annotations

import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.acceptance import (
    AcceptanceContractError,
    aggregate_formal_acceptance,
    load_acceptance_contract,
)
from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "config/scalar-piercing-acceptance.json"
SCALAR_CONTRACT = load_acceptance_contract(CONTRACT_PATH)
HEX_64 = "a" * 64
IMAGE_DIGEST = f"sha256:{'b' * 64}"
CINDERX_COMMIT = "f" * 40
CINDERX_BASE_IMAGE_DIGEST = f"sha256:{'4' * 64}"
CINDERX_WHEEL_SHA256 = "5" * 64


def _test_proof(
    *,
    gate_id: str,
    tier: str,
    required_tests: list[str],
    test_count: int,
) -> dict[str, object]:
    argv = ["python", "-m", "unittest", "-v", gate_id]
    argv_sha256 = hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    fields = {
        "gate_id": gate_id,
        "tier": tier,
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "source_git_commit": "e" * 40,
        "argv_sha256": argv_sha256,
        "log_sha256": "a" * 64,
        "required_tests": required_tests,
        "test_count": test_count,
        "skipped": 0,
    }
    proof_sha256 = hashlib.sha256(
        json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": 1,
        "status": "pass",
        **fields,
        "argv": argv,
        "duration_seconds": 1.0,
        "proof_sha256": proof_sha256,
    }


def _auth_proof() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "dashboard": {
                "published_bindings": [
                    {
                        "host_ip": "127.0.0.1",
                        "host_port": 8265,
                        "container_port": 8265,
                        "protocol": "tcp",
                    }
                ],
                "published_non_dashboard_ports": [],
                "non_loopback_connect": "refused",
                "requests": {
                    "unauthenticated": 401,
                    "wrong_token": 403,
                    "authenticated": 200,
                },
                "token_file_mode": "0600",
            },
            "secret_scan": {
                "scanned_artifact_count": 12,
                "scanned_image_count": 1,
                "token_matches": 0,
                "token_in_image_environment": False,
                "token_in_image_history": False,
                "token_in_retained_reports": False,
            },
        }
    )


def _cleanup_proof() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "before": {
                "routes_sha256": "4" * 64,
                "firewall_sha256": "5" * 64,
                "firewalld_runtime_sha256": "a" * 64,
                "firewalld_permanent_sha256": "b" * 64,
                "firewall_backend": "nftables-stateless",
                "firewalld_state": "running",
            },
            "after": {
                "routes_sha256": "4" * 64,
                "firewall_sha256": "5" * 64,
                "firewalld_runtime_sha256": "a" * 64,
                "firewalld_permanent_sha256": "b" * 64,
                "firewall_backend": "nftables-stateless",
                "firewalld_state": "running",
            },
            "cleanup": {
                "removed_container_ids": [
                    "6" * 64,
                    "7" * 64,
                    "8" * 64,
                ],
                "removed_network_ids": ["9" * 64, "a" * 64],
                "remaining_project_containers": [],
                "remaining_project_networks": [],
                "dashboard_port_open": False,
                "token_exists": False,
                "bridge_accommodation": {
                    "action": "runtime-trusted",
                    "network_id": "9" * 64,
                    "bridge_interface": f"br-{'9' * 12}",
                    "zone": "trusted",
                    "scope": "runtime",
                    "connectivity_before": {
                        "ray-worker-1": False,
                        "ray-worker-2": False,
                    },
                    "connectivity_after": {
                        "ray-worker-1": True,
                        "ray-worker-2": True,
                    },
                    "binding_added": True,
                    "binding_removed": True,
                    "bridge_interface_exists_after_cleanup": False,
                },
            },
        }
    )


def _hygiene_proof() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "evidence_hygiene": {
                "retained_reports": [
                    {
                        "name": "base-report.json",
                        "mode": "0600",
                        "sha256": "b" * 64,
                    }
                ],
                "raw_event_files_remaining": [],
                "raw_event_files_removed": ["off-events.jsonl", "auto-events.jsonl"],
            },
        }
    )


def _invalidation_proof() -> dict[str, object]:
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "source_git_commit": "e" * 40,
            "probe": {
                "changed_role": "ray-worker-2",
                "before_boot_id": "worker-2-before",
                "after_boot_id": "worker-2-after",
                "head_unchanged": True,
                "other_worker_unchanged": True,
                "manifest_unchanged": True,
                "image_unchanged": True,
                "invalidated_verdict": "inconclusive",
                "reason_code": "phase_identity_drift",
            },
            "artifacts": {
                "before_snapshot_sha256": "c" * 64,
                "after_snapshot_sha256": "d" * 64,
                "invalidated_report_sha256": "e" * 64,
            },
        }
    )


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
            "cinderx_commit": CINDERX_COMMIT,
            "cinderx_base_image_digest": CINDERX_BASE_IMAGE_DIGEST,
            "cinderx_wheel_sha256": CINDERX_WHEEL_SHA256,
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
    cinderx_proof = {
        "schema_version": 1,
        "status": "pass",
        "identity": {
            "cinderx_commit": CINDERX_COMMIT,
            "source_tree_sha256": "2" * 64,
            "patch_sha256": "3" * 64,
            "cinderx_wheel_sha256": CINDERX_WHEEL_SHA256,
            "image_digest": CINDERX_BASE_IMAGE_DIGEST,
            "python_version": "3.14.3",
            "soabi": "cpython-314-aarch64-linux-gnu",
            "py_enable_shared": 0,
            "python_library": "/opt/python314/lib/libpython3.14.a",
        },
        "runtime_tests": {
            "normal": {"passed": 1177, "failed": 0},
            "lightweight_frames_deopt": {"passed": 66, "failed": 0},
            "osr": {"passed": 130, "failed": 0},
            "udf_cases": [
                "UdfDataIntrinsicTest.RuntimeHelpersEnforceBorrowAndLifetime",
                "UdfDataIntrinsicTest.RuntimeHelpersRejectCrossProcessCapsule",
                "UdfDataIntrinsicTest.ExactGuardedLoadProducesPrimitiveHIR",
                "UdfDataIntrinsicTest.HIRMetadataMatchesPrimitiveRead",
                "UdfDataIntrinsicTest.LIRCallsFloat64SlotLoadHelper",
                "UdfDataIntrinsicHIRTest.ParserPrinterAndOutputTypePreserveGuardedPrimitiveLoad",
            ],
        },
        "python_tests": {
            "release_pytest": {
                "passed": 1332,
                "failed": 0,
                "errors": 0,
                "skipped": 63,
                "deselected": 8,
            },
            "adaptive_libtest": {
                "module_count": 456,
                "mode": "frame-eval-adaptive-aware",
                "runner": "dispatcher",
                "adaptive_compile_after": 24,
                "returncode": 0,
            },
            "official_skip_libtest": {
                "module_count": 26,
                "mode": "frame-eval-adaptive-aware",
                "runner": "dispatcher",
                "adaptive_compile_after": 24,
                "returncode": 0,
            },
            "udf_data_intrinsic": {
                "passed": 6,
                "subtests_passed": 22,
                "failed": 0,
            },
        },
        "artifacts": {
            name: str(index) * 64
            for index, name in enumerate(
                (
                    "fingerprint",
                    "runtime_log",
                    "release_log",
                    "adaptive_summary",
                    "adaptive_log",
                    "official_summary",
                    "official_log",
                    "targeted_log",
                ),
                start=1,
            )
        },
    }
    cinderx_material = {
        name: cinderx_proof[name]
        for name in (
            "identity",
            "runtime_tests",
            "python_tests",
            "artifacts",
        )
    }
    cinderx_proof["proof_sha256"] = hashlib.sha256(
        json.dumps(
            cinderx_material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    return {
        "run_id": "u13-run",
        "cluster_epoch": "u13-epoch",
        "source": {
            "git_commit": "e" * 40,
            "dirty": False,
            "cinderx_commit": CINDERX_COMMIT,
            "cinderx_source_tree_sha256": "2" * 64,
            "cinderx_patch_sha256": "3" * 64,
            "cinderx_wheel_sha256": CINDERX_WHEEL_SHA256,
            "cinderx_base_image_digest": CINDERX_BASE_IMAGE_DIGEST,
            "image_digest": IMAGE_DIGEST,
            "udf_jit_wheel_sha256": HEX_64,
        },
        "base_report": _base_report(),
        "black_box": {
            "off": _black_box("off"),
            "auto": _black_box("auto"),
        },
        "infrastructure": {
            "cinderx": cinderx_proof,
            "environment_auth": _auth_proof(),
            "environment_cleanup": _cleanup_proof(),
            "evidence_hygiene": _hygiene_proof(),
            "invalidation": _invalidation_proof(),
            "python_test_proofs": {
                "unit": _test_proof(
                    gate_id="python.unit",
                    tier="unit",
                    required_tests=list(
                        SCALAR_CONTRACT.test_suites["unit"].required_tests
                    ),
                    test_count=(
                        SCALAR_CONTRACT.test_suites[
                            "unit"
                        ].expected_test_count
                    ),
                ),
                "integration": _test_proof(
                    gate_id="python.integration",
                    tier="integration",
                    required_tests=list(
                        SCALAR_CONTRACT.test_suites[
                            "integration"
                        ].required_tests
                    ),
                    test_count=(
                        SCALAR_CONTRACT.test_suites[
                            "integration"
                        ].expected_test_count
                    ),
                ),
                "live": _test_proof(
                    gate_id="python.live",
                    tier="system",
                    required_tests=list(
                        SCALAR_CONTRACT.test_suites["live"].required_tests
                    ),
                    test_count=(
                        SCALAR_CONTRACT.test_suites[
                            "live"
                        ].expected_test_count
                    ),
                ),
            },
        },
        "measurement": {
            "schema_version": 1,
            "run_id": "u13-run",
            "cluster_epoch": "u13-epoch",
            "measurement_scope": "small_e2e_validation_not_release_performance",
            "units": "nanoseconds",
            "sample_count": 3,
            "warmup_count": 1,
            "environment": {
                "python_version": "3.14.3",
                "platform_machine": "aarch64",
                "daft_version": "0.7.2",
                "ray_version": "2.55.0",
                "pyarrow_version": "22.0.0",
                "manifest_sha256": "4" * 64,
            },
            "off": {
                "warmup_ns": 10,
                "samples_ns": [8, 9, 10],
                "median_ns": 9,
                "result_digest": "5" * 64,
            },
            "auto": {
                "cold_compile_window_ns": 12,
                "samples_ns": [7, 8, 9],
                "median_ns": 8,
                "result_digest": "5" * 64,
            },
            "result_equivalent": True,
            "speedup_gate_applied": False,
            "notes": ["validation only", "not a release claim"],
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
            if gate.tier == "system":
                self.assertTrue(
                    any(
                        target.startswith("tests.system.")
                        for target in gate.test_targets
                    ),
                    f"{gate_id} lacks a black-box system-test target",
                )
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
        evidence = _evidence()
        del evidence["infrastructure"]["environment_cleanup"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn("gate_missing:environment.cleanup", report["reason_codes"])

        evidence = _evidence()
        evidence["infrastructure"]["environment_auth"]["dashboard"][
            "non_loopback_connect"
        ] = "connected"
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:environment.auth_loopback",
            report["reason_codes"],
        )

        evidence = _evidence()
        evidence["infrastructure"]["evidence_hygiene"][
            "evidence_hygiene"
        ]["raw_event_files_remaining"] = ["auto-events.jsonl"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:evidence.permissions_cleanup",
            report["reason_codes"],
        )

        evidence = _evidence()
        del evidence["infrastructure"]["invalidation"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn(
            "gate_missing:evidence.invalidation_negative",
            report["reason_codes"],
        )

        for proof_name, gate in (
            ("unit", "tests.unit_suite"),
            ("integration", "tests.integration_suite"),
            ("live", "tests.live_suite"),
        ):
            with self.subTest(proof=proof_name):
                evidence = _evidence()
                del evidence["infrastructure"]["python_test_proofs"][
                    proof_name
                ]
                report = aggregate_formal_acceptance(self.contract, evidence)
                self.assertEqual(report["verdict"], "incomplete")
                self.assertIn(
                    f"gate_missing:{gate}",
                    report["reason_codes"],
                )

        evidence = _evidence()
        del evidence["infrastructure"]["cinderx"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn(
            "gate_missing:integration.cinderx_runtime",
            report["reason_codes"],
        )
        self.assertIn(
            "gate_missing:provenance.cinderx_source_identity",
            report["reason_codes"],
        )

    def test_cinderx_totals_or_source_identity_cannot_be_fabricated(self) -> None:
        evidence = _evidence()
        evidence["infrastructure"]["cinderx"]["runtime_tests"]["normal"][
            "passed"
        ] = 1175
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:integration.cinderx_runtime",
            report["reason_codes"],
        )

        evidence = _evidence()
        evidence["source"]["cinderx_patch_sha256"] = "9" * 64
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:provenance.cinderx_source_identity",
            report["reason_codes"],
        )

        evidence = _evidence()
        evidence["source"]["cinderx_wheel_sha256"] = "9" * 64
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:provenance.cinderx_source_identity",
            report["reason_codes"],
        )

        evidence = _evidence()
        evidence["source"]["cinderx_base_image_digest"] = (
            f"sha256:{'9' * 64}"
        )
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:provenance.cinderx_source_identity",
            report["reason_codes"],
        )

    def test_measurement_must_be_raw_identity_bound_equivalent_evidence(self) -> None:
        evidence = _evidence()
        evidence["measurement"]["auto"]["result_digest"] = "9" * 64
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "fail")
        self.assertIn(
            "gate_failed:measurement.non_gating",
            report["reason_codes"],
        )

        evidence = _evidence()
        del evidence["measurement"]["sample_count"]
        report = aggregate_formal_acceptance(self.contract, evidence)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertIn(
            "gate_missing:measurement.non_gating",
            report["reason_codes"],
        )

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
