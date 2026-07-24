from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_STATUSES = frozenset({"pass", "fail", "inconclusive", "incomplete", "stop"})
_TIERS = frozenset({"unit", "integration", "system"})
_EXPECTED_REQUIREMENTS = frozenset(f"R{index}" for index in range(1, 21))
_EXPECTED_ACCEPTANCE_EXAMPLES = frozenset(f"AE{index}" for index in range(1, 9))


class AcceptanceContractError(ValueError):
    """Formal acceptance input was malformed or outside the locked contract."""


@dataclass(frozen=True)
class AcceptanceGate:
    tier: str
    description: str
    test_targets: tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceContract:
    profile: str
    gates: Mapping[str, AcceptanceGate]
    requirements: Mapping[str, tuple[str, ...]]
    acceptance_examples: Mapping[str, tuple[str, ...]]


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
        ):
            raise AcceptanceContractError(f"{field}_{identifier}_gates_invalid")
        result[str(identifier)] = tuple(raw_gates)
    return MappingProxyType(result)


def load_acceptance_contract(path: str | Path) -> AcceptanceContract:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise AcceptanceContractError("contract_schema_version_invalid")
    raw_gates = _mapping(document.get("gates"), "contract_gates")
    gates: dict[str, AcceptanceGate] = {}
    for gate_id, raw_gate in raw_gates.items():
        gate = _mapping(raw_gate, f"gate_{gate_id}")
        tier = _string(gate.get("tier"), f"gate_{gate_id}_tier")
        if tier not in _TIERS:
            raise AcceptanceContractError(f"gate_{gate_id}_tier_invalid")
        targets = gate.get("test_targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target for target in targets)
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
    requirements = _identifier_map(
        document.get("requirements"),
        field="requirements",
        expected=_EXPECTED_REQUIREMENTS,
        gates=frozen_gates,
    )
    acceptance_examples = _identifier_map(
        document.get("acceptance_examples"),
        field="acceptance_examples",
        expected=_EXPECTED_ACCEPTANCE_EXAMPLES,
        gates=frozen_gates,
    )
    used_gates = {
        gate
        for gate_list in (*requirements.values(), *acceptance_examples.values())
        for gate in gate_list
    }
    if used_gates != set(frozen_gates):
        raise AcceptanceContractError("contract_contains_unmapped_gates")
    return AcceptanceContract(
        profile=_string(document.get("profile"), "contract_profile"),
        gates=frozen_gates,
        requirements=requirements,
        acceptance_examples=acceptance_examples,
    )


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
    image_identity = (
        "pass"
        if isinstance(source_image, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source_image) is not None
        and _valid_sha256(source_wheel)
        and source_image == manifest.get("image_digest")
        and source_wheel == manifest.get("udf_jit_wheel_sha256")
        else "fail"
    )
    return clean_source, image_identity


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

    with_column = _equivalent_scenario(
        off, auto, "with_column", require_non_empty=True
    )
    with_columns = _equivalent_scenario(
        off, auto, "with_columns", require_non_empty=True
    )
    cleanup = _combine(
        *(
            _external_boolean(infrastructure, field)
            for field in (
                "containers_removed",
                "networks_removed",
                "dashboard_port_closed",
                "firewall_restored",
                "routes_restored",
                "token_removed",
            )
        )
    )
    auth = _combine(
        *(
            _external_boolean(infrastructure, field)
            for field in ("dashboard_loopback_only", "other_ray_ports_unpublished")
        ),
        (
            "pass"
            if infrastructure.get("dashboard_unauthenticated_status") == 401
            and infrastructure.get("dashboard_wrong_token_status") == 403
            and infrastructure.get("dashboard_authenticated_status") == 200
            else (
                "incomplete"
                if any(
                    field not in infrastructure
                    for field in (
                        "dashboard_unauthenticated_status",
                        "dashboard_wrong_token_status",
                        "dashboard_authenticated_status",
                    )
                )
                else "fail"
            )
        ),
    )
    permissions_cleanup = _combine(
        *(
            _external_boolean(infrastructure, field)
            for field in (
                "raw_events_removed",
                "report_permissions_0600",
                "token_removed",
            )
        )
    )
    cinderx_runtime = _combine(
        _external_boolean(infrastructure, "cinderx_runtime_tests"),
        _external_boolean(infrastructure, "cinderx_python_tests"),
    )
    measurement_gate = _combine(
        _external_boolean(measurement, "completed"),
        _external_boolean(measurement, "semantic_equivalent"),
        (
            "pass"
            if measurement.get("speedup_gate_applied") is False
            else (
                "incomplete"
                if "speedup_gate_applied" not in measurement
                else "fail"
            )
        ),
    )
    statuses = {
        "provenance.clean_source": clean_source,
        "provenance.image_identity": image_identity,
        "tests.unit_suite": _external_boolean(
            infrastructure, "python_unit_tests"
        ),
        "tests.integration_suite": _external_boolean(
            infrastructure, "python_integration_tests"
        ),
        "tests.live_suite": _external_boolean(
            infrastructure, "live_tests_executed"
        ),
        "environment.locked_manifest": _base_check(base, "manifest"),
        "environment.three_node_topology": _base_check(base, "readiness"),
        "environment.auth_loopback": auth,
        "environment.secret_hygiene": _external_boolean(
            infrastructure, "secret_hygiene"
        ),
        "environment.cleanup": cleanup,
        "integration.object_store_data_plane": _external_boolean(
            infrastructure, "object_store_data_plane"
        ),
        "integration.cinderx_runtime": cinderx_runtime,
        "integration.worker_pool_qualification": _base_check(
            base, "worker_pool_qualification"
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
        "evidence.no_head_data_plane": _base_check(
            base, "data_plane_isolation"
        ),
        "evidence.permissions_cleanup": permissions_cleanup,
        "measurement.non_gating": measurement_gate,
    }
    if set(statuses) != set(contract.gates):
        raise AcceptanceContractError("aggregator_contract_gate_drift")
    return statuses


def _mapped_status(
    gate_statuses: Mapping[str, str], mappings: Mapping[str, tuple[str, ...]]
) -> dict[str, str]:
    return {
        identifier: _combine(*(gate_statuses[gate] for gate in gates))
        for identifier, gates in mappings.items()
    }


def aggregate_formal_acceptance(
    contract: AcceptanceContract, evidence: Mapping[str, Any]
) -> dict[str, object]:
    """Aggregate U13 UT/IT/ST proof; absent proof is never treated as pass."""

    gate_statuses = _gate_statuses(contract, evidence)
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
            "image_digest": evidence["source"].get("image_digest", ""),
            "udf_jit_wheel_sha256": evidence["source"].get(
                "udf_jit_wheel_sha256", ""
            ),
        },
    }
