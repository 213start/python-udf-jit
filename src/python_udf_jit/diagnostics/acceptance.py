from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from python_udf_jit.diagnostics.cinderx_evidence import (
    EXPECTED_UDF_RUNTIME_CASES,
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
_TIERS = frozenset({"unit", "integration", "system"})
_EXPECTED_REQUIREMENTS = frozenset(f"R{index}" for index in range(1, 21))
_EXPECTED_ACCEPTANCE_EXAMPLES = frozenset(f"AE{index}" for index in range(1, 9))
UNIT_REQUIRED_TESTS = (
    "test_contract_traces_every_requirement_and_acceptance_example",
    "test_exact_static_runtime_and_python_gates_produce_proof",
    "test_fixture_never_imports_or_calls_plugin_internals",
    "test_outer_guard_miss_falls_back_once_without_compile_or_semantic_execute",
    "test_rejects_non_internal_data_plane",
    "test_runtime_binding_is_exact_and_reversible",
)
INTEGRATION_REQUIRED_TESTS = (
    "test_inline_artifact_bytes_survive_the_wrapper_worker_roundtrip",
    "test_exact_live_topology_is_accepted",
    "test_partitioned_float_projection_runs_only_on_worker_nodes",
    "test_head_owned_object_ref_reaches_both_workers",
    "test_both_workers_execute_region_driven_cinderx_scalar_load",
    "test_same_production_plan_compiles_and_executes_on_each_worker",
)
LIVE_REQUIRED_TESTS = (
    "test_ae1_to_ae8_pass_and_keep_natural_coverage_separate",
    "test_live_evidence_is_supplied_by_the_external_harness",
)


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
    )
    required_identity = (
        "cinderx_commit",
        "source_tree_sha256",
        "patch_sha256",
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
    valid = (
        validate_cinderx_evidence(raw_proof) == "pass"
        and isinstance(cinderx_commit, str)
        and _GIT_COMMIT.fullmatch(cinderx_commit) is not None
        and _valid_sha256(source_tree)
        and _valid_sha256(patch)
        and raw_proof.get("schema_version") == 1
        and raw_proof.get("status") == "pass"
        and _valid_sha256(raw_proof.get("proof_sha256"))
        and identity.get("cinderx_commit")
        == manifest.get("cinderx_commit")
        == cinderx_commit
        and identity.get("source_tree_sha256") == source_tree
        and identity.get("patch_sha256") == patch
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
    runtime = raw_proof.get("runtime_tests")
    python_tests = raw_proof.get("python_tests")
    artifacts = raw_proof.get("artifacts")
    if (
        not isinstance(runtime, Mapping)
        or not isinstance(python_tests, Mapping)
        or not isinstance(artifacts, Mapping)
    ):
        return "incomplete"
    required_runtime = (
        "normal",
        "lightweight_frames_deopt",
        "osr",
        "udf_cases",
    )
    required_python = (
        "release_pytest",
        "adaptive_libtest",
        "official_skip_libtest",
        "udf_data_intrinsic",
    )
    if (
        any(field not in runtime for field in required_runtime)
        or any(field not in python_tests for field in required_python)
        or len(artifacts) != 8
    ):
        return "incomplete"

    expected_counts = {
        "normal": 1176,
        "lightweight_frames_deopt": 66,
        "osr": 130,
    }
    runtime_valid = all(
        isinstance(runtime.get(name), Mapping)
        and runtime[name].get("passed") == count
        and runtime[name].get("failed") == 0
        for name, count in expected_counts.items()
    )
    release = python_tests["release_pytest"]
    adaptive = python_tests["adaptive_libtest"]
    official = python_tests["official_skip_libtest"]
    targeted = python_tests["udf_data_intrinsic"]
    python_valid = (
        isinstance(release, Mapping)
        and release.get("passed") == 1331
        and release.get("failed") == 0
        and release.get("errors") == 0
        and isinstance(adaptive, Mapping)
        and adaptive.get("module_count") == 456
        and adaptive.get("returncode") == 0
        and isinstance(official, Mapping)
        and official.get("module_count") == 26
        and official.get("returncode") == 0
        and isinstance(targeted, Mapping)
        and targeted.get("passed") == 5
        and targeted.get("failed") == 0
    )
    valid = (
        validate_cinderx_evidence(raw_proof) == "pass"
        and runtime_valid
        and runtime.get("udf_cases") == list(EXPECTED_UDF_RUNTIME_CASES)
        and python_valid
        and all(_valid_sha256(value) for value in artifacts.values())
    )
    return "pass" if valid else "fail"


def _python_test_statuses(
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
    unit = validate_unittest_evidence(
        proofs.get("unit"),
        gate_id="python.unit",
        tier="unit",
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=UNIT_REQUIRED_TESTS,
        minimum_test_count=117,
        allow_skips=False,
    )
    integration = validate_unittest_evidence(
        proofs.get("integration"),
        gate_id="python.integration",
        tier="integration",
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=INTEGRATION_REQUIRED_TESTS,
        minimum_test_count=29,
        allow_skips=False,
    )
    live = validate_unittest_evidence(
        proofs.get("live"),
        gate_id="python.live",
        tier="system",
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=source_git_commit,
        required_tests=LIVE_REQUIRED_TESTS,
        minimum_test_count=12,
        allow_skips=False,
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
            "cinderx_commit": evidence["source"].get(
                "cinderx_commit", ""
            ),
            "cinderx_source_tree_sha256": evidence["source"].get(
                "cinderx_source_tree_sha256", ""
            ),
            "cinderx_patch_sha256": evidence["source"].get(
                "cinderx_patch_sha256", ""
            ),
            "image_digest": evidence["source"].get("image_digest", ""),
            "udf_jit_wheel_sha256": evidence["source"].get(
                "udf_jit_wheel_sha256", ""
            ),
        },
    }
