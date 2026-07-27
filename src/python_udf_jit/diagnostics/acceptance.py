from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from python_udf_jit.diagnostics.cinderx_evidence import (
    validate_cinderx_evidence,
)
from python_udf_jit.diagnostics.environment_evidence import (
    validate_auth_evidence,
    validate_cleanup_evidence,
    validate_hygiene_evidence,
    validate_secret_evidence,
)
from python_udf_jit.diagnostics.invalidation_evidence import (
    validate_invalidation_evidence,
)
from python_udf_jit.diagnostics.test_evidence import (
    validate_unittest_evidence,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_STATUSES = frozenset({"pass", "fail", "inconclusive", "incomplete", "stop"})
_GATE_TIERS = frozenset({"unit", "integration", "system", "release"})
_TIER_ORDER = ("unit", "integration", "system", "release")
_EXPECTED_REQUIREMENTS = frozenset(f"R{index}" for index in range(1, 21))
_EXPECTED_MAINLINE_REQUIREMENTS = frozenset(
    f"R{index}" for index in range(1, 26)
)
_EXPECTED_ACCEPTANCE_EXAMPLES = frozenset(f"AE{index}" for index in range(1, 9))
_EXPECTED_MAINLINE_ACCEPTANCE_EXAMPLES = frozenset(
    f"AE{index}" for index in range(1, 13)
)
_EXPECTED_MAINLINE_RFCS = frozenset(
    f"RFC-{index:03d}" for index in range(1, 9)
)
_DISABLED_ADVANCED_RFCS = tuple(
    f"RFC-{index:03d}" for index in range(9, 13)
)
_SUPPORT_COMPONENTS = frozenset(
    {"python", "cinderx", "daft", "ray", "lance", "pyarrow"}
)
_SCALAR_PROFILE = "u13-formal-scalar-mainline-acceptance"
_MAINLINE_PROFILE = "mainline-production"
_SCALAR_SCHEMA = "urn:python-udf-jit:scalar-piercing-acceptance:v1"
_MAINLINE_SCHEMA = "urn:python-udf-jit:mainline-release-prerequisites:v1"
_SUITE_INVENTORY_FILE = "acceptance-suite-inventories.json"


class AcceptanceContractError(ValueError):
    """Formal acceptance input was malformed or outside the locked contract."""


class CompletionStatus(StrEnum):
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class GateOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    STOP = "stop"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class AcceptanceGate:
    tier: str
    description: str
    test_targets: tuple[str, ...]


@dataclass(frozen=True)
class TestSuiteContract:
    gate_id: str
    tier: str
    arguments: tuple[str, ...]
    required_tests: tuple[str, ...]
    expected_test_count: int
    allow_skips: bool


@dataclass(frozen=True)
class AcceptanceContract:
    schema_id: str
    profile: str
    gates: Mapping[str, AcceptanceGate]
    requirements: Mapping[str, tuple[str, ...]]
    acceptance_examples: Mapping[str, tuple[str, ...]]
    rfc_gates: Mapping[str, tuple[str, ...]]
    disabled_rfcs: tuple[str, ...]
    test_suites: Mapping[str, TestSuiteContract]
    test_tier_mapping: Mapping[str, tuple[str, ...]]
    support_matrix_sha256: str
    separates_unit_lifecycle: bool


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AcceptanceContractError(f"{field}_invalid")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceContractError(f"{field}_invalid")
    return value


def _identifier_map(
    value: object,
    *,
    field: str,
    expected: frozenset[str],
    gates: Mapping[str, AcceptanceGate],
) -> Mapping[str, tuple[str, ...]]:
    document = _mapping(value, field)
    if set(document) != expected:
        raise AcceptanceContractError(f"{field}_identifiers_invalid")
    result: dict[str, tuple[str, ...]] = {}
    for identifier, raw_gates in document.items():
        if (
            not isinstance(raw_gates, list)
            or not raw_gates
            or not all(isinstance(gate, str) and gate in gates for gate in raw_gates)
            or len(set(raw_gates)) != len(raw_gates)
        ):
            raise AcceptanceContractError(f"{field}_{identifier}_gates_invalid")
        result[str(identifier)] = tuple(raw_gates)
    return MappingProxyType(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise AcceptanceContractError(f"duplicate_key:{key}")
        document[key] = value
    return document


def _load_document(path: Path, field: str) -> dict[str, Any]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except AcceptanceContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceContractError(f"{field}_unreadable") from error
    if not isinstance(document, dict):
        raise AcceptanceContractError(f"{field}_invalid")
    return document


def _strings(
    value: object,
    field: str,
    *,
    require_unique: bool,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or (require_unique and len(set(value)) != len(value))
    ):
        raise AcceptanceContractError(f"{field}_invalid")
    return tuple(value)


def _test_suite_contracts(
    value: object,
) -> Mapping[str, TestSuiteContract]:
    suites = _mapping(value, "test_suites")
    if set(suites) != {"unit", "integration", "live"}:
        raise AcceptanceContractError("test_suites_invalid")
    result: dict[str, TestSuiteContract] = {}
    expected_tiers = {
        "unit": "unit",
        "integration": "integration",
        "live": "system",
    }
    for name, raw_suite in suites.items():
        suite = _mapping(raw_suite, f"test_suite_{name}")
        if set(suite) != {
            "gate_id",
            "tier",
            "arguments",
            "required_tests",
            "expected_test_count",
            "allow_skips",
        }:
            raise AcceptanceContractError(
                f"test_suite_{name}_fields_invalid"
            )
        expected_count = suite.get("expected_test_count")
        allow_skips = suite.get("allow_skips")
        if type(expected_count) is not int or expected_count < 1:
            raise AcceptanceContractError(
                f"test_suite_{name}_expected_test_count_invalid"
            )
        if type(allow_skips) is not bool:
            raise AcceptanceContractError(
                f"test_suite_{name}_allow_skips_invalid"
            )
        tier = _string(suite.get("tier"), f"test_suite_{name}_tier")
        if tier != expected_tiers[name]:
            raise AcceptanceContractError(f"test_suite_{name}_tier_invalid")
        required_tests = _strings(
            suite.get("required_tests"),
            f"test_suite_{name}_required_tests",
            require_unique=True,
        )
        if expected_count < len(required_tests):
            raise AcceptanceContractError(
                f"test_suite_{name}_expected_test_count_invalid"
            )
        result[str(name)] = TestSuiteContract(
            gate_id=_string(
                suite.get("gate_id"),
                f"test_suite_{name}_gate_id",
            ),
            tier=tier,
            arguments=_strings(
                suite.get("arguments"),
                f"test_suite_{name}_arguments",
                require_unique=False,
            ),
            required_tests=required_tests,
            expected_test_count=expected_count,
            allow_skips=allow_skips,
        )
    return MappingProxyType(result)


def _profile_suite_inventory(
    contract_path: Path,
    profile: str,
) -> Mapping[str, TestSuiteContract]:
    inventory = _load_document(
        contract_path.parent / _SUITE_INVENTORY_FILE,
        "suite_inventory",
    )
    if (
        set(inventory) != {"schema_version", "profiles"}
        or inventory.get("schema_version") != 1
    ):
        raise AcceptanceContractError("suite_inventory_schema_version_invalid")
    profiles = _mapping(inventory.get("profiles"), "suite_inventory_profiles")
    if set(profiles) != {_SCALAR_PROFILE}:
        raise AcceptanceContractError("suite_inventory_profiles_invalid")
    selected = _mapping(
        profiles.get(profile),
        "suite_inventory_profile",
    )
    return _test_suite_contracts(selected.get("test_suites"))


def load_acceptance_contract(
    path: str | Path,
    *,
    expected_profile: str | None = None,
    schema_path: str | Path | None = None,
) -> AcceptanceContract:
    contract_path = Path(path)
    document = _load_document(contract_path, "contract")
    profile = _string(document.get("profile"), "contract_profile")
    profile_schemas = {
        _SCALAR_PROFILE: _SCALAR_SCHEMA,
        _MAINLINE_PROFILE: _MAINLINE_SCHEMA,
    }
    if profile not in profile_schemas:
        raise AcceptanceContractError("contract_profile_unknown")
    if expected_profile is not None and profile != expected_profile:
        raise AcceptanceContractError("contract_profile_mismatch")
    schema_id = document.get("contract_schema")
    if schema_id is None and profile == _SCALAR_PROFILE:
        schema_id = _SCALAR_SCHEMA
    if schema_id != profile_schemas[profile]:
        raise AcceptanceContractError("contract_schema_unknown")
    expected_schema_version = 1 if profile == _SCALAR_PROFILE else 2
    if document.get("schema_version") != expected_schema_version:
        raise AcceptanceContractError("contract_schema_version_invalid")
    if schema_path is not None:
        schema = _load_document(Path(schema_path), "contract_schema")
        if schema.get("$id") != schema_id:
            raise AcceptanceContractError("contract_schema_identity_mismatch")
    if profile == _MAINLINE_PROFILE and document.get("$schema") != (
        "mainline-release-prerequisites.schema.json"
    ):
        raise AcceptanceContractError("contract_schema_reference_invalid")
    expected_fields = (
        {
            "schema_version",
            "profile",
            "gates",
            "requirements",
            "acceptance_examples",
        }
        if profile == _SCALAR_PROFILE
        else {
            "$schema",
            "schema_version",
            "contract_schema",
            "profile",
            "support_matrix",
            "support_matrix_sha256",
            "unit_completion_values",
            "gate_outcome_values",
            "performance_policy",
            "performance_baseline",
            "formal_performance_qualification",
            "disabled_rfcs",
            "test_suites",
            "required_gates",
            "gates",
            "requirements",
            "rfc_gates",
            "test_tier_mapping",
            "acceptance_examples",
        }
    )
    if set(document) != expected_fields:
        raise AcceptanceContractError("contract_fields_invalid")

    raw_gates = _mapping(document.get("gates"), "contract_gates")
    if not raw_gates:
        raise AcceptanceContractError("contract_gates_invalid")
    gates: dict[str, AcceptanceGate] = {}
    for gate_id, raw_gate in raw_gates.items():
        if not isinstance(gate_id, str) or not gate_id:
            raise AcceptanceContractError("gate_id_invalid")
        gate = _mapping(raw_gate, f"gate_{gate_id}")
        if set(gate) != {"tier", "description", "test_targets"}:
            raise AcceptanceContractError(f"gate_{gate_id}_fields_invalid")
        tier = _string(gate.get("tier"), f"gate_{gate_id}_tier")
        if tier not in _GATE_TIERS:
            raise AcceptanceContractError(f"gate_{gate_id}_tier_invalid")
        targets = gate.get("test_targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target for target in targets)
            or len(set(targets)) != len(targets)
        ):
            raise AcceptanceContractError(f"gate_{gate_id}_targets_invalid")
        gates[str(gate_id)] = AcceptanceGate(
            tier=tier,
            description=_string(
                gate.get("description"), f"gate_{gate_id}_description"
            ),
            test_targets=tuple(targets),
        )
    frozen_gates = MappingProxyType(gates)
    expected_requirements = (
        _EXPECTED_REQUIREMENTS
        if profile == _SCALAR_PROFILE
        else _EXPECTED_MAINLINE_REQUIREMENTS
    )
    if profile == _MAINLINE_PROFILE:
        required_gates = _strings(
            document.get("required_gates"),
            "required_gates",
            require_unique=True,
        )
        if set(required_gates) != set(frozen_gates):
            raise AcceptanceContractError("required_gates_invalid")
        raw_rfc_gates = _mapping(document.get("rfc_gates"), "rfc_gates")
        disabled_rfcs = _strings(
            document.get("disabled_rfcs"),
            "disabled_rfcs",
            require_unique=True,
        )
        if (
            disabled_rfcs != _DISABLED_ADVANCED_RFCS
            or set(raw_rfc_gates) & set(_DISABLED_ADVANCED_RFCS)
        ):
            raise AcceptanceContractError("advanced_rfcs_must_be_disabled")
    requirements = _identifier_map(
        document.get("requirements"),
        field="requirements",
        expected=expected_requirements,
        gates=frozen_gates,
    )
    acceptance_examples = _identifier_map(
        document.get("acceptance_examples"),
        field="acceptance_examples",
        expected=(
            _EXPECTED_ACCEPTANCE_EXAMPLES
            if profile == _SCALAR_PROFILE
            else _EXPECTED_MAINLINE_ACCEPTANCE_EXAMPLES
        ),
        gates=frozen_gates,
    )
    used_gates = {
        gate
        for gate_list in (*requirements.values(), *acceptance_examples.values())
        for gate in gate_list
    }
    if used_gates != set(frozen_gates):
        raise AcceptanceContractError("contract_contains_unmapped_gates")
    if profile == _MAINLINE_PROFILE:
        rfc_gates = _identifier_map(
            raw_rfc_gates,
            field="rfc_gates",
            expected=_EXPECTED_MAINLINE_RFCS,
            gates=frozen_gates,
        )
        raw_tiers = _mapping(
            document.get("test_tier_mapping"),
            "test_tier_mapping",
        )
        if set(raw_tiers) != expected_requirements:
            raise AcceptanceContractError("test_tier_mapping_identifiers_invalid")
        tiers: dict[str, tuple[str, ...]] = {}
        for identifier, raw_values in raw_tiers.items():
            values = _strings(
                raw_values,
                f"test_tier_mapping_{identifier}",
                require_unique=True,
            )
            expected_tiers = tuple(
                tier
                for tier in _TIER_ORDER
                if tier
                in {
                    frozen_gates[gate].tier
                    for gate in requirements[str(identifier)]
                }
            )
            if (
                not set(values) <= _GATE_TIERS
                or values != expected_tiers
            ):
                raise AcceptanceContractError(
                    f"test_tier_mapping_{identifier}_invalid"
                )
            tiers[str(identifier)] = values
        test_tier_mapping: Mapping[str, tuple[str, ...]] = MappingProxyType(
            tiers
        )
        support_matrix_sha256 = _string(
            document.get("support_matrix_sha256"),
            "support_matrix_sha256",
        )
        if _SHA256.fullmatch(support_matrix_sha256) is None:
            raise AcceptanceContractError("support_matrix_sha256_invalid")
        if document.get("support_matrix") != "mainline-support-matrix.json":
            raise AcceptanceContractError("support_matrix_reference_invalid")
        if document.get("unit_completion_values") != [
            "incomplete",
            "complete",
        ]:
            raise AcceptanceContractError("unit_completion_values_invalid")
        if document.get("gate_outcome_values") != [
            "pass",
            "fail",
            "stop",
            "inconclusive",
        ]:
            raise AcceptanceContractError("gate_outcome_values_invalid")
        performance = _mapping(
            document.get("performance_policy"),
            "performance_policy",
        )
        if (
            set(performance)
            != {
                "target_speedup",
                "default_mode",
                "target_applies_to",
                "blocks_functional_completion",
                "below_target_disposition",
            }
            or performance.get("blocks_functional_completion") is not False
            or performance.get("below_target_disposition")
            != "record_non_blocking_result"
            or performance.get("default_mode") != "directional"
            or performance.get("target_applies_to") != "formal_only"
            or not isinstance(
                performance.get("target_speedup"),
                (int, float),
            )
            or isinstance(performance.get("target_speedup"), bool)
            or float(performance["target_speedup"]) <= 0
        ):
            raise AcceptanceContractError("performance_policy_invalid")
        baseline = _mapping(
            document.get("performance_baseline"),
            "performance_baseline",
        )
        baseline_source = _mapping(
            baseline.get("baseline"),
            "performance_baseline_source",
        )
        baseline_candidate = _mapping(
            baseline.get("candidate"),
            "performance_baseline_candidate",
        )
        if (
            set(baseline)
            != {
                "mode",
                "workload",
                "approximate_rows",
                "baseline",
                "candidate",
                "environment_constraint",
                "off_runs",
                "auto_runs",
                "conclusion_scope",
                "correctness",
                "blocks_functional_completion",
            }
            or set(baseline_source) != {"mode", "execution"}
            or set(baseline_candidate)
            != {"mode", "enabled_rfcs", "disabled_rfcs"}
            or baseline.get("workload") != "tpch_sf10_lineitem_scalar_q6"
            or type(baseline.get("approximate_rows")) is not int
            or baseline["approximate_rows"] <= 0
            or baseline_source.get("mode") != "off"
            or baseline_source.get("execution")
            != "original_daft_scalar_udf"
            or baseline_candidate.get("mode") != "auto"
            or baseline_candidate.get("enabled_rfcs")
            != [f"RFC-{index:03d}" for index in range(1, 9)]
            or baseline_candidate.get("disabled_rfcs")
            != list(_DISABLED_ADVANCED_RFCS)
            or baseline.get("mode") != "directional"
            or baseline.get("environment_constraint") != "same_environment"
            or baseline.get("off_runs") != 1
            or baseline.get("auto_runs") != 1
            or baseline.get("conclusion_scope") != "directional_only"
            or baseline.get("correctness")
            != "ordered_result_and_aggregate_hash_equal"
            or baseline.get("blocks_functional_completion") is not False
        ):
            raise AcceptanceContractError("performance_baseline_invalid")
        formal = _mapping(
            document.get("formal_performance_qualification"),
            "formal_performance_qualification",
        )
        if (
            set(formal)
            != {
                "mode",
                "cli_flag",
                "warmup_runs",
                "alternating_measured_runs",
                "statistic",
                "stability_metrics",
                "target_speedup",
                "fallback_minimum_ratio",
                "blocks_functional_completion",
            }
            or formal.get("mode") != "formal"
            or formal.get("cli_flag") != "--formal"
            or formal.get("warmup_runs") != 1
            or formal.get("alternating_measured_runs") != 5
            or formal.get("statistic") != "median"
            or formal.get("stability_metrics") != ["mad", "drift"]
            or formal.get("target_speedup")
            != performance.get("target_speedup")
            or formal.get("fallback_minimum_ratio") != 0.98
            or formal.get("blocks_functional_completion") is not False
        ):
            raise AcceptanceContractError(
                "formal_performance_qualification_invalid"
            )
        separates_unit_lifecycle = True
    else:
        rfc_gates = MappingProxyType({})
        disabled_rfcs = ()
        test_tier_mapping = MappingProxyType({})
        support_matrix_sha256 = ""
        separates_unit_lifecycle = False
    test_suites = (
        _test_suite_contracts(document.get("test_suites"))
        if "test_suites" in document
        else _profile_suite_inventory(contract_path, profile)
    )
    return AcceptanceContract(
        schema_id=str(schema_id),
        profile=profile,
        gates=frozen_gates,
        requirements=requirements,
        acceptance_examples=acceptance_examples,
        rfc_gates=rfc_gates,
        disabled_rfcs=disabled_rfcs,
        test_suites=test_suites,
        test_tier_mapping=test_tier_mapping,
        support_matrix_sha256=support_matrix_sha256,
        separates_unit_lifecycle=separates_unit_lifecycle,
    )


def _missing_prerequisite_paths(
    section: Mapping[str, Any],
    *,
    prefix: str,
    required_paths: tuple[tuple[str, ...], ...],
) -> list[str]:
    missing: list[str] = []
    for path in required_paths:
        value: object = section
        for component in path:
            if not isinstance(value, Mapping) or component not in value:
                value = None
                break
            value = value[component]
        if value is None or value == "":
            missing.append(".".join((prefix, *path)))
    return missing


def _prerequisite_date(
    value: object,
    *,
    field: str,
    issues: list[str],
) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        issues.append(f"{field}:date_invalid")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        issues.append(f"{field}:date_invalid")
        return None


def _prerequisite_nonnegative_int(
    value: object,
    *,
    field: str,
    issues: list[str],
    allow_zero: bool,
) -> int | None:
    if value is None:
        return None
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        issues.append(f"{field}:integer_invalid")
        return None
    return value


def _current_support_issues(
    section: Mapping[str, Any],
    *,
    expected_components: set[str],
) -> list[str]:
    issues: list[str] = []
    delivery = _mapping(
        section.get("delivery_window"),
        "current_component_support_delivery_window",
    )
    adoption = _mapping(
        section.get("first_adoption_window"),
        "current_component_support_first_adoption_window",
    )
    delivery_start = _prerequisite_date(
        delivery.get("starts_on"),
        field="current_component_support.delivery_window.starts_on",
        issues=issues,
    )
    delivery_end = _prerequisite_date(
        delivery.get("ends_on"),
        field="current_component_support.delivery_window.ends_on",
        issues=issues,
    )
    adoption_start = _prerequisite_date(
        adoption.get("starts_on"),
        field="current_component_support.first_adoption_window.starts_on",
        issues=issues,
    )
    adoption_end = _prerequisite_date(
        adoption.get("ends_on"),
        field="current_component_support.first_adoption_window.ends_on",
        issues=issues,
    )
    verified_on = _prerequisite_date(
        section.get("verified_on"),
        field="current_component_support.verified_on",
        issues=issues,
    )
    remaining = _prerequisite_nonnegative_int(
        section.get("remaining_support_days"),
        field="current_component_support.remaining_support_days",
        issues=issues,
        allow_zero=True,
    )
    minimum = _prerequisite_nonnegative_int(
        section.get("minimum_remaining_support_days"),
        field="current_component_support.minimum_remaining_support_days",
        issues=issues,
        allow_zero=False,
    )
    if (
        delivery_start is not None
        and delivery_end is not None
        and delivery_start > delivery_end
    ):
        issues.append(
            "current_component_support.delivery_window:range_invalid"
        )
    if (
        adoption_start is not None
        and adoption_end is not None
        and adoption_start > adoption_end
    ):
        issues.append(
            "current_component_support.first_adoption_window:range_invalid"
        )
    if (
        delivery_start is not None
        and adoption_start is not None
        and adoption_start < delivery_start
    ):
        issues.append(
            "current_component_support.first_adoption_window:"
            "starts_before_delivery"
        )
    if (
        remaining is not None
        and minimum is not None
        and remaining < minimum
    ):
        issues.append(
            "current_component_support.remaining_support_days:"
            "below_minimum"
        )

    component_evidence = _mapping(
        section.get("component_evidence"),
        "current_component_support_component_evidence",
    )
    component_remaining: list[int] = []
    for component in sorted(expected_components):
        evidence = _mapping(
            component_evidence.get(component),
            f"current_component_support_component_{component}",
        )
        prefix = f"current_component_support.component_evidence.{component}"
        support_end = _prerequisite_date(
            evidence.get("support_ends_on"),
            field=f"{prefix}.support_ends_on",
            issues=issues,
        )
        component_verified = _prerequisite_date(
            evidence.get("verified_on"),
            field=f"{prefix}.verified_on",
            issues=issues,
        )
        component_days = _prerequisite_nonnegative_int(
            evidence.get("remaining_support_days"),
            field=f"{prefix}.remaining_support_days",
            issues=issues,
            allow_zero=True,
        )
        if (
            support_end is not None
            and component_verified is not None
            and component_days is not None
            and component_days != (support_end - component_verified).days
        ):
            issues.append(f"{prefix}.remaining_support_days:mismatch")
        if (
            component_verified is not None
            and verified_on is not None
            and component_verified != verified_on
        ):
            issues.append(f"{prefix}.verified_on:snapshot_mismatch")
        if (
            support_end is not None
            and adoption_end is not None
            and support_end < adoption_end
        ):
            issues.append(f"{prefix}.support_ends_on:before_adoption_end")
        if (
            component_days is not None
            and minimum is not None
            and component_days < minimum
        ):
            issues.append(f"{prefix}.remaining_support_days:below_minimum")
        if component_days is not None:
            component_remaining.append(component_days)
    if (
        remaining is not None
        and len(component_remaining) == len(expected_components)
        and remaining != min(component_remaining)
    ):
        issues.append(
            "current_component_support.remaining_support_days:"
            "component_minimum_mismatch"
        )
    return issues


def evaluate_mainline_prerequisites(
    matrix: Mapping[str, Any],
) -> dict[str, object]:
    """Evaluate declared external prerequisites without inventing evidence."""

    root = _mapping(matrix, "support_matrix")
    if root.get("profile") != _MAINLINE_PROFILE:
        raise AcceptanceContractError("support_matrix_profile_invalid")
    prerequisites = _mapping(
        root.get("release_prerequisites"),
        "release_prerequisites",
    )
    blocking_requirements = {
        "current_component_support": (
            ("delivery_window", "starts_on"),
            ("delivery_window", "ends_on"),
            ("first_adoption_window", "starts_on"),
            ("first_adoption_window", "ends_on"),
            ("remaining_support_days",),
            ("minimum_remaining_support_days",),
            ("lifecycle_owner",),
            ("verified_on",),
        ),
        "multi_node_environment": (
            ("owner",),
            ("reservation", "reservation_id"),
            ("reservation", "scheduled_for"),
            ("deadline",),
            ("network_owner",),
            ("identity_owner",),
            ("external_evidence",),
        ),
        "credential_distribution": (
            ("owner",),
            ("channel_provider",),
            ("deadline",),
            ("external_evidence",),
        ),
        "emergency_disable_distribution": (
            ("owner",),
            ("channel_provider",),
            ("deadline",),
            ("blue98_evidence",),
            ("physical_multinode_evidence",),
        ),
    }
    missing: list[str] = []
    gates: dict[str, str] = {}
    for name, required_paths in blocking_requirements.items():
        section = _mapping(
            prerequisites.get(name),
            f"release_prerequisite_{name}",
        )
        if name == "current_component_support":
            component_evidence = _mapping(
                section.get("component_evidence"),
                "current_component_support_component_evidence",
            )
            if set(component_evidence) != _SUPPORT_COMPONENTS:
                raise AcceptanceContractError(
                    "current_component_support_components_invalid"
                )
            required_paths = (
                *required_paths,
                *(
                    ("component_evidence", component, field)
                    for component in sorted(_SUPPORT_COMPONENTS)
                    for field in (
                        "support_ends_on",
                        "remaining_support_days",
                        "verified_on",
                        "source",
                    )
                ),
            )
        section_missing = _missing_prerequisite_paths(
            section,
            prefix=name,
            required_paths=required_paths,
        )
        semantic_issues = (
            _current_support_issues(
                section,
                expected_components=set(_SUPPORT_COMPONENTS),
            )
            if name == "current_component_support"
            else []
        )
        blockers = [*section_missing, *semantic_issues]
        expected_status = "incomplete" if blockers else "complete"
        expected_outcome = "stop" if blockers else "pass"
        if (
            section.get("status") != expected_status
            or section.get("gate_outcome") != expected_outcome
        ):
            raise AcceptanceContractError(
                f"release_prerequisite_false_claim:{name}"
            )
        missing.extend(blockers)
        gates[name] = expected_outcome

    adopter = _mapping(
        prerequisites.get("first_adopter"),
        "release_prerequisite_first_adopter",
    )
    adopter_missing = _missing_prerequisite_paths(
        adopter,
        prefix="first_adopter",
        required_paths=(
            ("business_owner",),
            ("named_job",),
            ("job_fingerprint",),
            ("target_cluster",),
            ("off_baseline_evidence",),
            ("scalar_matrix_coverage",),
            ("verified_on",),
        ),
    )
    expected_adopter_status = (
        "incomplete" if adopter_missing else "complete"
    )
    # Registration alone never authorizes optimized canary execution.
    # Observe/shadow evidence, a rollback plan, and change authorization are
    # separate U11/U13 gates.
    expected_rollout_ceiling = "observe-ready"
    if (
        adopter.get("status") != expected_adopter_status
        or adopter.get("rollout_ceiling") != expected_rollout_ceiling
        or adopter.get("blocks_functional_completion") is not False
    ):
        raise AcceptanceContractError(
            "release_prerequisite_false_claim:first_adopter"
        )
    missing.extend(adopter_missing)

    next_baseline = _mapping(
        prerequisites.get("next_baseline_trial"),
        "release_prerequisite_next_baseline_trial",
    )
    if (
        next_baseline.get("blocks_u2") is not False
        or next_baseline.get("blocks_current_functional_work") is not False
    ):
        raise AcceptanceContractError(
            "next_baseline_trial_must_be_non_blocking"
        )

    return {
        "unit_completion_status": (
            "complete"
            if all(outcome == "pass" for outcome in gates.values())
            else "incomplete"
        ),
        "gates": gates,
        "missing": sorted(missing),
        "rollout_ceiling": expected_rollout_ceiling,
        "non_blocking_tracking": {
            "next_baseline_trial": next_baseline.get("status"),
            "first_adopter": expected_adopter_status,
        },
    }


def load_mainline_prerequisite_report(
    contract_path: str | Path,
    *,
    contract: AcceptanceContract | None = None,
) -> dict[str, object]:
    """Load the hash-bound support matrix and evaluate external prerequisites."""

    path = Path(contract_path)
    selected = contract or load_acceptance_contract(path)
    if selected.profile != _MAINLINE_PROFILE:
        raise AcceptanceContractError(
            "mainline_prerequisites_require_mainline_profile"
        )
    matrix_path = path.parent / "mainline-support-matrix.json"
    try:
        payload = matrix_path.read_bytes()
    except OSError as error:
        raise AcceptanceContractError(
            "support_matrix_unreadable"
        ) from error
    if hashlib.sha256(payload).hexdigest() != selected.support_matrix_sha256:
        raise AcceptanceContractError("support_matrix_hash_mismatch")
    matrix = _load_document(matrix_path, "support_matrix")
    return evaluate_mainline_prerequisites(matrix)


def _external_boolean(document: Mapping[str, Any], field: str) -> str:
    if field not in document:
        return "incomplete"
    return "pass" if document[field] is True else "fail"


def _combine(*statuses: str) -> str:
    if any(status not in _STATUSES for status in statuses):
        raise AcceptanceContractError("gate_status_invalid")
    for status in ("stop", "fail", "inconclusive", "incomplete"):
        if status in statuses:
            return status
    return "pass"


def _base_check(base: Mapping[str, Any], name: str) -> str:
    checks = _mapping(base.get("checks"), "base_checks")
    value = checks.get(name)
    return str(value) if value in _STATUSES else "incomplete"


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _scenario(
    observation: Mapping[str, Any], name: str
) -> Mapping[str, Any] | None:
    scenarios = observation.get("scenarios")
    if not isinstance(scenarios, Mapping):
        return None
    value = scenarios.get(name)
    return value if isinstance(value, Mapping) else None


def _equivalent_scenario(
    off: Mapping[str, Any],
    auto: Mapping[str, Any],
    name: str,
    *,
    require_non_empty: bool,
) -> str:
    off_scenario = _scenario(off, name)
    auto_scenario = _scenario(auto, name)
    if off_scenario is None or auto_scenario is None:
        return "incomplete"
    fields = (
        "ordered_result_sha256",
        "schema_sha256",
        "row_count",
        "callable_calls",
        "side_effect_count",
    )
    if any(field not in off_scenario or field not in auto_scenario for field in fields):
        return "incomplete"
    if (
        off_scenario.get("completed") is not True
        or auto_scenario.get("completed") is not True
        or not _valid_sha256(off_scenario["ordered_result_sha256"])
        or not _valid_sha256(off_scenario["schema_sha256"])
        or any(off_scenario[field] != auto_scenario[field] for field in fields)
    ):
        return "fail"
    try:
        row_count = int(off_scenario["row_count"])
        callable_calls = int(off_scenario["callable_calls"])
        side_effect_count = int(off_scenario["side_effect_count"])
    except (TypeError, ValueError):
        return "fail"
    if row_count < 0 or callable_calls < 0 or side_effect_count < 0:
        return "fail"
    if require_non_empty and row_count == 0:
        return "fail"
    return "pass"


def _unsupported_semantics(
    off: Mapping[str, Any], auto: Mapping[str, Any]
) -> str:
    status = _equivalent_scenario(
        off, auto, "unsupported", require_non_empty=True
    )
    if status != "pass":
        return status
    scenario = _scenario(auto, "unsupported")
    assert scenario is not None
    return (
        "pass"
        if scenario["callable_calls"]
        == scenario["side_effect_count"]
        == scenario["row_count"]
        else "fail"
    )


def _exception_semantics(
    off: Mapping[str, Any], auto: Mapping[str, Any]
) -> str:
    off_scenario = _scenario(off, "exception")
    auto_scenario = _scenario(auto, "exception")
    if off_scenario is None or auto_scenario is None:
        return "incomplete"
    fields = (
        "exception_type",
        "user_exception_type_observed",
        "message_sentinel_observed",
        "callable_calls",
        "side_effect_count",
    )
    if any(field not in off_scenario or field not in auto_scenario for field in fields):
        return "incomplete"
    if (
        off_scenario.get("completed") is not True
        or auto_scenario.get("completed") is not True
        or any(off_scenario[field] != auto_scenario[field] for field in fields)
        or off_scenario["user_exception_type_observed"] is not True
        or off_scenario["message_sentinel_observed"] is not True
        or off_scenario["callable_calls"] != 1
        or off_scenario["side_effect_count"] != 1
    ):
        return "fail"
    return "pass"


def _zero_row_semantics(
    off: Mapping[str, Any], auto: Mapping[str, Any]
) -> str:
    status = _equivalent_scenario(off, auto, "zero_row", require_non_empty=False)
    if status != "pass":
        return status
    scenario = _scenario(auto, "zero_row")
    assert scenario is not None
    return (
        "pass"
        if scenario["row_count"]
        == scenario["callable_calls"]
        == scenario["side_effect_count"]
        == 0
        else "fail"
    )


def _black_box_observations(
    evidence: Mapping[str, Any], *, run_id: str, cluster_epoch: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    black_box = _mapping(evidence.get("black_box"), "black_box")
    off = _mapping(black_box.get("off"), "black_box_off")
    auto = _mapping(black_box.get("auto"), "black_box_auto")
    if off.get("mode") != "off" or auto.get("mode") != "auto":
        raise AcceptanceContractError("black_box_modes_invalid")
    for name, observation in (("off", off), ("auto", auto)):
        if (
            observation.get("schema_version") != 1
            or observation.get("run_id") != run_id
            or observation.get("cluster_epoch") != cluster_epoch
        ):
            raise AcceptanceContractError(f"black_box_{name}_identity_invalid")
    return off, auto


def _transparent_bootstrap(
    off: Mapping[str, Any], auto: Mapping[str, Any]
) -> str:
    required = (
        "user_script_sha256",
        "plugin_import_count",
        "bootstrap_hooks_installed",
    )
    if any(field not in off or field not in auto for field in required):
        return "incomplete"
    return (
        "pass"
        if _valid_sha256(off["user_script_sha256"])
        and off["user_script_sha256"] == auto["user_script_sha256"]
        and off["plugin_import_count"] == auto["plugin_import_count"] == 0
        and off["bootstrap_hooks_installed"] is False
        and auto["bootstrap_hooks_installed"] is True
        else "fail"
    )


def _source_gates(
    source: Mapping[str, Any], base: Mapping[str, Any]
) -> tuple[str, str]:
    clean_source = (
        "pass"
        if _GIT_COMMIT.fullmatch(str(source.get("git_commit", ""))) is not None
        and source.get("dirty") is False
        else "fail"
    )
    manifest = base.get("manifest")
    if not isinstance(manifest, Mapping):
        return clean_source, "incomplete"
    source_image = source.get("image_digest")
    source_wheel = source.get("udf_jit_wheel_sha256")
    source_cinderx_wheel = source.get("cinderx_wheel_sha256")
    source_cinderx_base = source.get("cinderx_base_image_digest")
    image_identity = (
        "pass"
        if isinstance(source_image, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source_image) is not None
        and _valid_sha256(source_wheel)
        and _valid_sha256(source_cinderx_wheel)
        and isinstance(source_cinderx_base, str)
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            source_cinderx_base,
        )
        is not None
        and source_image == manifest.get("image_digest")
        and source_wheel == manifest.get("udf_jit_wheel_sha256")
        and source_cinderx_wheel
        == manifest.get("cinderx_wheel_sha256")
        and source_cinderx_base
        == manifest.get("cinderx_base_image_digest")
        else "fail"
    )
    return clean_source, image_identity


def _cinderx_source_identity(
    source: Mapping[str, Any],
    base: Mapping[str, Any],
    infrastructure: Mapping[str, Any],
) -> str:
    raw_proof = infrastructure.get("cinderx")
    manifest = base.get("manifest")
    if not isinstance(raw_proof, Mapping) or not isinstance(manifest, Mapping):
        return "incomplete"
    identity = raw_proof.get("identity")
    required_source = (
        "cinderx_commit",
        "cinderx_source_tree_sha256",
        "cinderx_patch_sha256",
        "cinderx_wheel_sha256",
        "cinderx_base_image_digest",
    )
    required_identity = (
        "cinderx_commit",
        "source_tree_sha256",
        "patch_sha256",
        "cinderx_wheel_sha256",
        "image_digest",
        "python_version",
        "soabi",
        "py_enable_shared",
        "python_library",
    )
    if (
        any(field not in source for field in required_source)
        or not isinstance(identity, Mapping)
        or any(field not in identity for field in required_identity)
        or "cinderx_commit" not in manifest
    ):
        return "incomplete"
    cinderx_commit = source["cinderx_commit"]
    source_tree = source["cinderx_source_tree_sha256"]
    patch = source["cinderx_patch_sha256"]
    cinderx_wheel = source["cinderx_wheel_sha256"]
    cinderx_base_image = source["cinderx_base_image_digest"]
    valid = (
        validate_cinderx_evidence(raw_proof) == "pass"
        and isinstance(cinderx_commit, str)
        and _GIT_COMMIT.fullmatch(cinderx_commit) is not None
        and _valid_sha256(source_tree)
        and _valid_sha256(patch)
        and _valid_sha256(cinderx_wheel)
        and isinstance(cinderx_base_image, str)
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            cinderx_base_image,
        )
        is not None
        and raw_proof.get("schema_version") == 1
        and raw_proof.get("status") == "pass"
        and _valid_sha256(raw_proof.get("proof_sha256"))
        and identity.get("cinderx_commit")
        == manifest.get("cinderx_commit")
        == cinderx_commit
        and identity.get("source_tree_sha256") == source_tree
        and identity.get("patch_sha256") == patch
        and identity.get("cinderx_wheel_sha256")
        == manifest.get("cinderx_wheel_sha256")
        == cinderx_wheel
        and identity.get("image_digest")
        == manifest.get("cinderx_base_image_digest")
        == cinderx_base_image
        and isinstance(identity.get("image_digest"), str)
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(identity["image_digest"])
        )
        is not None
        and identity.get("python_version") == "3.14.3"
        and identity.get("soabi") == "cpython-314-aarch64-linux-gnu"
        and identity.get("py_enable_shared") == 0
        and identity.get("python_library")
        == "/opt/python314/lib/libpython3.14.a"
    )
    return "pass" if valid else "fail"


def _cinderx_test_status(infrastructure: Mapping[str, Any]) -> str:
    raw_proof = infrastructure.get("cinderx")
    if not isinstance(raw_proof, Mapping):
        return "incomplete"
    return validate_cinderx_evidence(raw_proof)


def _python_test_statuses(
    contract: AcceptanceContract,
    infrastructure: Mapping[str, Any],
    *,
    run_id: str,
    cluster_epoch: str,
    source_git_commit: object,
) -> tuple[str, str, str]:
    proofs = infrastructure.get("python_test_proofs")
    if not isinstance(proofs, Mapping):
        return "incomplete", "incomplete", "incomplete"
    if (
        not isinstance(source_git_commit, str)
        or _GIT_COMMIT.fullmatch(source_git_commit) is None
    ):
        return "fail", "fail", "fail"
    unit_contract = contract.test_suites["unit"]
    integration_contract = contract.test_suites["integration"]
    live_contract = contract.test_suites["live"]
    unit = validate_unittest_evidence(
        proofs.get("unit"),
        gate_id=unit_contract.gate_id,
        tier=unit_contract.tier,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=unit_contract.required_tests,
        minimum_test_count=unit_contract.expected_test_count,
        expected_test_count=unit_contract.expected_test_count,
        allow_skips=unit_contract.allow_skips,
    )
    integration = validate_unittest_evidence(
        proofs.get("integration"),
        gate_id=integration_contract.gate_id,
        tier=integration_contract.tier,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=integration_contract.required_tests,
        minimum_test_count=integration_contract.expected_test_count,
        expected_test_count=integration_contract.expected_test_count,
        allow_skips=integration_contract.allow_skips,
    )
    live = validate_unittest_evidence(
        proofs.get("live"),
        gate_id=live_contract.gate_id,
        tier=live_contract.tier,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=live_contract.required_tests,
        minimum_test_count=live_contract.expected_test_count,
        expected_test_count=live_contract.expected_test_count,
        allow_skips=live_contract.allow_skips,
    )
    return unit, integration, live


def _measurement_status(
    measurement: Mapping[str, Any],
    *,
    run_id: str,
    cluster_epoch: str,
    base: Mapping[str, Any],
) -> str:
    required = (
        "schema_version",
        "run_id",
        "cluster_epoch",
        "measurement_scope",
        "units",
        "sample_count",
        "warmup_count",
        "environment",
        "off",
        "auto",
        "result_equivalent",
        "speedup_gate_applied",
    )
    if any(field not in measurement for field in required):
        return "incomplete"
    environment = measurement["environment"]
    off = measurement["off"]
    auto = measurement["auto"]
    manifest = base.get("manifest")
    if (
        not isinstance(environment, Mapping)
        or not isinstance(off, Mapping)
        or not isinstance(auto, Mapping)
        or not isinstance(manifest, Mapping)
    ):
        return "fail"
    sample_count = measurement["sample_count"]
    off_samples = off.get("samples_ns")
    auto_samples = auto.get("samples_ns")
    expected_manifest = manifest.get("candidate_manifest_sha256")
    manifest_valid = (
        _valid_sha256(environment.get("manifest_sha256"))
        and (
            expected_manifest is None
            or environment.get("manifest_sha256") == expected_manifest
        )
    )
    valid = (
        measurement["schema_version"] == 1
        and measurement["run_id"] == run_id
        and measurement["cluster_epoch"] == cluster_epoch
        and measurement["measurement_scope"]
        == "small_e2e_validation_not_release_performance"
        and measurement["units"] == "nanoseconds"
        and isinstance(sample_count, int)
        and not isinstance(sample_count, bool)
        and 1 <= sample_count <= 20
        and measurement["warmup_count"] == 1
        and environment.get("python_version") == "3.14.3"
        and environment.get("platform_machine") == "aarch64"
        and environment.get("daft_version") == "0.7.2"
        and environment.get("ray_version") == "2.55.0"
        and environment.get("pyarrow_version") == "22.0.0"
        and manifest_valid
        and isinstance(off_samples, list)
        and isinstance(auto_samples, list)
        and len(off_samples) == len(auto_samples) == sample_count
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (*off_samples, *auto_samples)
        )
        and isinstance(off.get("warmup_ns"), int)
        and off["warmup_ns"] >= 0
        and isinstance(auto.get("cold_compile_window_ns"), int)
        and auto["cold_compile_window_ns"] >= 0
        and _valid_sha256(off.get("result_digest"))
        and off.get("result_digest") == auto.get("result_digest")
        and measurement["result_equivalent"] is True
        and measurement["speedup_gate_applied"] is False
    )
    return "pass" if valid else "fail"


def _gate_statuses(
    contract: AcceptanceContract, evidence: Mapping[str, Any]
) -> dict[str, str]:
    run_id = _string(evidence.get("run_id"), "acceptance_run_id")
    cluster_epoch = _string(
        evidence.get("cluster_epoch"), "acceptance_cluster_epoch"
    )
    base = _mapping(evidence.get("base_report"), "base_report")
    if base.get("run_id") != run_id or base.get("cluster_epoch") != cluster_epoch:
        raise AcceptanceContractError("base_report_identity_invalid")
    base_verdict = str(base.get("verdict", ""))
    if base_verdict not in _STATUSES - {"incomplete"}:
        raise AcceptanceContractError("base_report_verdict_invalid")
    off, auto = _black_box_observations(
        evidence, run_id=run_id, cluster_epoch=cluster_epoch
    )
    source = _mapping(evidence.get("source"), "source")
    infrastructure = _mapping(evidence.get("infrastructure"), "infrastructure")
    measurement = _mapping(evidence.get("measurement"), "measurement")
    clean_source, image_identity = _source_gates(source, base)
    cinderx_source_identity = _cinderx_source_identity(
        source, base, infrastructure
    )
    unit_tests, integration_tests, live_tests = _python_test_statuses(
        contract,
        infrastructure,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source.get("git_commit"),
    )
    invalidation_negative = validate_invalidation_evidence(
        infrastructure.get("invalidation"),
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=str(source.get("git_commit", "")),
    )

    with_column = _equivalent_scenario(
        off, auto, "with_column", require_non_empty=True
    )
    with_columns = _equivalent_scenario(
        off, auto, "with_columns", require_non_empty=True
    )
    cleanup = validate_cleanup_evidence(
        infrastructure.get("environment_cleanup"),
        run_id=run_id,
        cluster_epoch=cluster_epoch,
    )
    auth_proof = infrastructure.get("environment_auth")
    auth = validate_auth_evidence(
        auth_proof,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
    )
    secret_hygiene = validate_secret_evidence(
        auth_proof,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
    )
    permissions_cleanup = _combine(
        validate_hygiene_evidence(
            infrastructure.get("evidence_hygiene"),
            run_id=run_id,
            cluster_epoch=cluster_epoch,
        ),
        cleanup,
    )
    cinderx_runtime = _combine(
        _cinderx_test_status(infrastructure),
        integration_tests,
    )
    measurement_gate = _measurement_status(
        measurement,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        base=base,
    )
    statuses = {
        "provenance.clean_source": clean_source,
        "provenance.image_identity": image_identity,
        "provenance.cinderx_source_identity": cinderx_source_identity,
        "tests.unit_suite": unit_tests,
        "tests.integration_suite": integration_tests,
        "tests.live_suite": live_tests,
        "environment.locked_manifest": _base_check(base, "manifest"),
        "environment.three_node_topology": _base_check(base, "readiness"),
        "environment.auth_loopback": auth,
        "environment.secret_hygiene": secret_hygiene,
        "environment.cleanup": cleanup,
        "integration.object_store_data_plane": integration_tests,
        "integration.cinderx_runtime": cinderx_runtime,
        "integration.worker_pool_qualification": _combine(
            _base_check(base, "worker_pool_qualification"),
            integration_tests,
        ),
        "system.transparent_bootstrap": _transparent_bootstrap(off, auto),
        "system.supported_jit": _base_check(base, "supported_hit"),
        "system.with_column_semantics": with_column,
        "system.with_columns_semantics": with_columns,
        "system.ordered_results": _combine(with_column, with_columns),
        "system.guard_miss_semantics": _base_check(base, "guard_miss"),
        "system.unsupported_semantics": _combine(
            _base_check(base, "unsupported"),
            _unsupported_semantics(off, auto),
        ),
        "system.fail_open": _base_check(base, "fail_open"),
        "system.exception_semantics": _exception_semantics(off, auto),
        "system.zero_row_semantics": _combine(
            _base_check(base, "zero_row"),
            _zero_row_semantics(off, auto),
        ),
        "evidence.base_verdict": base_verdict,
        "evidence.compile_hit_chain": _base_check(base, "supported_hit"),
        "evidence.attempt_identity": _base_check(base, "attempt_attribution"),
        "evidence.phase_identity": _base_check(base, "evidence_identity"),
        "evidence.invalidation_negative": invalidation_negative,
        "evidence.no_head_data_plane": _base_check(
            base, "data_plane_isolation"
        ),
        "evidence.permissions_cleanup": permissions_cleanup,
        "measurement.non_gating": measurement_gate,
    }
    raw_prerequisites = evidence.get("mainline_prerequisites")
    if raw_prerequisites is not None:
        prerequisites = _mapping(
            raw_prerequisites,
            "mainline_prerequisites",
        )
        prerequisite_gates = _mapping(
            prerequisites.get("gates"),
            "mainline_prerequisite_gates",
        )
        prerequisite_mapping = {
            "current_component_support":
                "prerequisite.current_component_support",
            "credential_distribution":
                "prerequisite.credential_distribution",
            "emergency_disable_distribution":
                "prerequisite.emergency_distribution",
        }
        for source_name, gate_id in prerequisite_mapping.items():
            raw_status = prerequisite_gates.get(source_name)
            if raw_status not in {"pass", "stop"}:
                raise AcceptanceContractError(
                    f"mainline_prerequisite_outcome_invalid:{source_name}"
                )
            if gate_id in contract.gates:
                statuses[gate_id] = str(raw_status)
    unexpected = set(statuses) - set(contract.gates)
    if unexpected:
        raise AcceptanceContractError("aggregator_contract_gate_drift")
    suite_statuses = {
        "unit": unit_tests,
        "integration": integration_tests,
        "system": live_tests,
    }
    suite_names = {
        "unit": "unit",
        "integration": "integration",
        "system": "live",
    }
    for gate_id in sorted(set(contract.gates) - set(statuses)):
        gate = contract.gates[gate_id]
        if gate.tier == "release":
            statuses[gate_id] = "incomplete"
            continue
        suite = contract.test_suites[suite_names[gate.tier]]
        required_names = set(suite.required_tests)
        target_names = {
            target.rsplit(".", 1)[-1] for target in gate.test_targets
        }
        statuses[gate_id] = (
            suite_statuses[gate.tier]
            if target_names <= required_names
            else "incomplete"
        )
    return statuses


def _mapped_status(
    gate_statuses: Mapping[str, str], mappings: Mapping[str, tuple[str, ...]]
) -> dict[str, str]:
    return {
        identifier: _combine(*(gate_statuses[gate] for gate in gates))
        for identifier, gates in mappings.items()
    }


def _combine_outcomes(*statuses: GateOutcome) -> GateOutcome:
    for status in (
        GateOutcome.STOP,
        GateOutcome.FAIL,
        GateOutcome.INCONCLUSIVE,
    ):
        if status in statuses:
            return status
    return GateOutcome.PASS


def aggregate_profile_outcomes(
    contract: AcceptanceContract,
    gate_outcomes: Mapping[str, str | GateOutcome],
    *,
    unit_completion_status: str | CompletionStatus,
) -> dict[str, object]:
    """Summarize release gates without treating unit lifecycle as an outcome."""

    if not set(gate_outcomes) <= set(contract.gates):
        raise AcceptanceContractError("gate_outcomes_identifiers_invalid")
    outcomes: dict[str, GateOutcome] = {}
    for gate, raw_status in gate_outcomes.items():
        try:
            outcomes[gate] = GateOutcome(raw_status)
        except ValueError as error:
            raise AcceptanceContractError(
                f"gate_outcome_invalid:{gate}"
            ) from error
    try:
        completion = CompletionStatus(unit_completion_status)
    except ValueError as error:
        raise AcceptanceContractError(
            "unit_completion_status_invalid"
        ) from error
    missing_gates = sorted(set(contract.gates) - set(outcomes))
    if missing_gates:
        completion = CompletionStatus.INCOMPLETE
    executed_verdict = (
        _combine_outcomes(*outcomes.values())
        if outcomes
        else None
    )
    verdict = None if missing_gates else executed_verdict
    reason_prefixes = {
        GateOutcome.FAIL: "gate_failed",
        GateOutcome.INCONCLUSIVE: "gate_inconclusive",
        GateOutcome.STOP: "gate_stopped",
    }
    reasons = [
        f"{reason_prefixes[status]}:{gate}"
        for gate, status in sorted(outcomes.items())
        if status is not GateOutcome.PASS
    ]

    def mapped(
        mappings: Mapping[str, tuple[str, ...]],
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        executed: dict[str, str] = {}
        missing: dict[str, list[str]] = {}
        for identifier, gates in mappings.items():
            absent = sorted(set(gates) - set(outcomes))
            if absent:
                missing[identifier] = absent
            else:
                executed[identifier] = _combine_outcomes(
                    *(outcomes[gate] for gate in gates)
                ).value
        return executed, missing

    requirements, missing_requirements = mapped(contract.requirements)
    acceptance_examples, missing_acceptance_examples = mapped(
        contract.acceptance_examples
    )
    rfcs, missing_rfcs = mapped(contract.rfc_gates)
    lifecycle_reasons = (
        []
        if completion is CompletionStatus.COMPLETE
        else ["unit_completion_incomplete"]
    )
    lifecycle_reasons.extend(
        f"required_gate_not_executed:{gate}" for gate in missing_gates
    )

    return {
        "profile": contract.profile,
        "contract_schema": contract.schema_id,
        "unit_completion_status": completion.value,
        "verdict": None if verdict is None else verdict.value,
        "executed_gate_verdict": (
            None if executed_verdict is None else executed_verdict.value
        ),
        "release_ready": (
            completion is CompletionStatus.COMPLETE
            and verdict is GateOutcome.PASS
            and not missing_gates
        ),
        "reason_codes": reasons,
        "lifecycle_reason_codes": lifecycle_reasons,
        "required_gate_count": len(contract.gates),
        "executed_gate_count": len(outcomes),
        "missing_gates": missing_gates,
        "missing_gate_reasons": {
            gate: "required_evidence_missing_or_not_executed"
            for gate in missing_gates
        },
        "gates": {
            gate: status.value for gate, status in outcomes.items()
        },
        "requirements": requirements,
        "missing_requirements": missing_requirements,
        "acceptance_examples": acceptance_examples,
        "missing_acceptance_examples": missing_acceptance_examples,
        "rfcs": rfcs,
        "missing_rfcs": missing_rfcs,
        "disabled_rfcs": list(contract.disabled_rfcs),
    }


def aggregate_formal_acceptance(
    contract: AcceptanceContract, evidence: Mapping[str, Any]
) -> dict[str, object]:
    """Aggregate U13 UT/IT/ST proof; absent proof is never treated as pass."""

    gate_statuses = _gate_statuses(contract, evidence)
    if contract.separates_unit_lifecycle:
        outcomes = {
            gate: GateOutcome(status)
            for gate, status in gate_statuses.items()
            if status != "incomplete"
        }
        report = aggregate_profile_outcomes(
            contract,
            outcomes,
            unit_completion_status=evidence.get(
                "unit_completion_status",
                CompletionStatus.INCOMPLETE,
            ),
        )
        report.update(
            {
                "schema_version": 2,
                "run_id": evidence["run_id"],
                "cluster_epoch": evidence["cluster_epoch"],
                "source": {
                    "git_commit": evidence["source"].get("git_commit", ""),
                    "cinderx_commit": evidence["source"].get(
                        "cinderx_commit", ""
                    ),
                    "cinderx_source_tree_sha256": evidence["source"].get(
                        "cinderx_source_tree_sha256", ""
                    ),
                    "cinderx_patch_sha256": evidence["source"].get(
                        "cinderx_patch_sha256", ""
                    ),
                    "cinderx_wheel_sha256": evidence["source"].get(
                        "cinderx_wheel_sha256", ""
                    ),
                    "cinderx_base_image_digest": evidence["source"].get(
                        "cinderx_base_image_digest", ""
                    ),
                    "image_digest": evidence["source"].get(
                        "image_digest", ""
                    ),
                    "udf_jit_wheel_sha256": evidence["source"].get(
                        "udf_jit_wheel_sha256", ""
                    ),
                },
            }
        )
        return report
    verdict = _combine(*gate_statuses.values())
    reason_prefixes = {
        "fail": "gate_failed",
        "inconclusive": "gate_inconclusive",
        "incomplete": "gate_missing",
        "stop": "gate_stopped",
    }
    reasons = [
        f"{reason_prefixes[status]}:{gate}"
        for gate, status in sorted(gate_statuses.items())
        if status != "pass"
    ]
    return {
        "schema_version": 1,
        "profile": contract.profile,
        "run_id": evidence["run_id"],
        "cluster_epoch": evidence["cluster_epoch"],
        "verdict": verdict,
        "reason_codes": reasons,
        "gates": gate_statuses,
        "requirements": _mapped_status(gate_statuses, contract.requirements),
        "acceptance_examples": _mapped_status(
            gate_statuses, contract.acceptance_examples
        ),
        "source": {
            "git_commit": evidence["source"].get("git_commit", ""),
            "cinderx_commit": evidence["source"].get(
                "cinderx_commit", ""
            ),
            "cinderx_source_tree_sha256": evidence["source"].get(
                "cinderx_source_tree_sha256", ""
            ),
            "cinderx_patch_sha256": evidence["source"].get(
                "cinderx_patch_sha256", ""
            ),
            "cinderx_wheel_sha256": evidence["source"].get(
                "cinderx_wheel_sha256", ""
            ),
            "cinderx_base_image_digest": evidence["source"].get(
                "cinderx_base_image_digest", ""
            ),
            "image_digest": evidence["source"].get("image_digest", ""),
            "udf_jit_wheel_sha256": evidence["source"].get(
                "udf_jit_wheel_sha256", ""
            ),
        },
    }
