from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset({"readiness", "qualification", "e2e"})
_SCENARIOS = frozenset(
    {
        "supported",
        "guard_miss",
        "unsupported",
        "mode_off",
        "fingerprint_mismatch",
        "corrupt_artifact",
        "zero_row",
    }
)
_STAGES = frozenset({"adapter", "capture", "artifact", "layout", "jit", "execute"})
_DECISIONS = frozenset(
    {
        "candidate_registered",
        "operation_finalized",
        "rejected",
        "semantic_reverify",
        "descriptor_bound",
        "compile",
        "hit",
        "fallback",
        "semantic_execute",
        "post_entry_failure",
        "fail_open",
    }
)
_REASON_CODES = frozenset(
    {
        "compatible_expression_call",
        "with_columns_projection",
        "unsupported_opcode",
        "unsupported_dependency",
        "opaque_call",
        "unsupported_original_callable",
        "verified",
        "scalar_slot_bound",
        "cinderx_force_compile_verified",
        "process_variant_cache",
        "compile_submitted",
        "compile_inflight",
        "success",
        "schema_mismatch",
        "artifact_mismatch",
        "mode_off",
        "with_columns_fingerprint_mismatch",
        "func_call_fingerprint_mismatch",
        "u2_fallback_only",
        "descriptor_epoch_mismatch",
    }
)
_ROLES = frozenset({"ray-head-driver", "ray-worker-1", "ray-worker-2"})
_EVENT_FIELDS = (
    "event_type",
    "phase",
    "scenario",
    "stage",
    "decision",
    "reason_code",
    "cluster_epoch",
    "run_id",
    "role",
    "node_id",
    "actor_id",
    "worker_id",
    "pid",
    "process_generation",
    "partition_id",
    "task_attempt",
    "variant_key",
    "artifact_hash",
    "code_hash",
    "execution_mode",
    "timestamp_ns",
)
_MANIFEST_FIELDS = (
    "candidate_manifest_sha256",
    "image_digest",
    "python_version",
    "cinderx_commit",
    "cinderx_base_image_digest",
    "cinderx_wheel_sha256",
    "soabi",
    "daft_version",
    "ray_version",
    "pyarrow_version",
    "udf_jit_wheel_sha256",
)
_TASK_STATE_FIELDS = frozenset(
    {
        "task_id",
        "job_id",
        "attempt_number",
        "state",
        "type",
        "actor_id",
        "node_id",
        "worker_id",
        "worker_pid",
        "name",
        "parent_task_id",
        "start_time_ms",
        "end_time_ms",
    }
)


class EvidenceContractError(ValueError):
    """Evidence was unsafe to persist or structurally outside the v1 contract."""


def _safe_id(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EvidenceContractError("identifier_type_invalid")
    if allow_empty and value == "":
        return ""
    if _SAFE_ID.fullmatch(value) is None:
        raise EvidenceContractError("identifier_format_invalid")
    return value


def _enum(value: object, choices: frozenset[str], reason: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise EvidenceContractError(reason)
    return value


def _hash_prefix(value: object, *, allow_empty: bool = True) -> str:
    if value == "" and allow_empty:
        return ""
    if not isinstance(value, str) or (
        _SHA256.fullmatch(value) is None
        and re.fullmatch(r"[0-9a-f]{16}", value) is None
    ):
        raise EvidenceContractError("hash_format_invalid")
    return value[:16]


def sanitize_event(event: Mapping[str, Any]) -> dict[str, object]:
    """Return the value-free event whitelist; unknown payload fields are dropped."""

    if not isinstance(event, Mapping):
        raise EvidenceContractError("event_type_invalid")
    safe: dict[str, object] = {
        "event_type": _enum(event.get("event_type"), frozenset({"driver", "runtime"}), "event_type_invalid"),
        "phase": _enum(event.get("phase"), _PHASES, "event_phase_invalid"),
        "scenario": _enum(event.get("scenario"), _SCENARIOS, "event_scenario_invalid"),
        "stage": _enum(event.get("stage"), _STAGES, "event_stage_invalid"),
        "decision": _enum(event.get("decision"), _DECISIONS, "event_decision_invalid"),
        "reason_code": _enum(event.get("reason_code"), _REASON_CODES, "event_reason_invalid"),
        "cluster_epoch": _safe_id(event.get("cluster_epoch")),
        "run_id": _safe_id(event.get("run_id")),
        "role": _enum(event.get("role"), _ROLES, "event_role_invalid"),
        "node_id": _safe_id(event.get("node_id")),
        "actor_id": _safe_id(event.get("actor_id", ""), allow_empty=True),
        "worker_id": _safe_id(event.get("worker_id", ""), allow_empty=True),
        "pid": int(event.get("pid", 0)),
        "process_generation": _safe_id(
            event.get("process_generation", ""), allow_empty=True
        ),
        "partition_id": _safe_id(event.get("partition_id", ""), allow_empty=True),
        "task_attempt": _safe_id(event.get("task_attempt", ""), allow_empty=True),
        "variant_key": _hash_prefix(event.get("variant_key", "")),
        "artifact_hash": _hash_prefix(event.get("artifact_hash", "")),
        "code_hash": _hash_prefix(event.get("code_hash", "")),
        "execution_mode": _safe_id(
            event.get("execution_mode", ""), allow_empty=True
        ),
        "timestamp_ns": int(event.get("timestamp_ns", 0)),
    }
    if safe["pid"] < 0 or safe["timestamp_ns"] < 0:
        raise EvidenceContractError("event_numeric_field_invalid")
    return {field: safe[field] for field in _EVENT_FIELDS}


def _manifest(document: object) -> dict[str, object] | None:
    if not isinstance(document, Mapping) or any(
        field not in document for field in _MANIFEST_FIELDS
    ):
        return None
    try:
        candidate = str(document["candidate_manifest_sha256"])
        wheel = str(document["udf_jit_wheel_sha256"])
        cinderx_wheel = str(document["cinderx_wheel_sha256"])
        image = str(document["image_digest"])
        cinderx_base_image = str(document["cinderx_base_image_digest"])
        if (
            _SHA256.fullmatch(candidate) is None
            or _SHA256.fullmatch(wheel) is None
            or _SHA256.fullmatch(cinderx_wheel) is None
        ):
            return None
        for digest in (image, cinderx_base_image):
            if (
                digest != f"sha256:{digest.removeprefix('sha256:')}"
                or _SHA256.fullmatch(digest.removeprefix("sha256:")) is None
            ):
                return None
        return {field: document[field] for field in _MANIFEST_FIELDS}
    except (TypeError, ValueError):
        return None


def _snapshot_identity(snapshot: Mapping[str, Any]) -> tuple[object, ...] | None:
    try:
        phase = _enum(snapshot["phase"], _PHASES, "phase_invalid")
        cluster_epoch = _safe_id(snapshot["cluster_epoch"])
        manifest = str(snapshot["manifest_sha256"])
        if _SHA256.fullmatch(manifest) is None:
            return None
        nodes = []
        for node in snapshot["nodes"]:
            nodes.append(
                (
                    _enum(node["role"], _ROLES, "role_invalid"),
                    _safe_id(node["node_id"]),
                    _safe_id(node["container_boot_id"]),
                )
            )
        return phase, cluster_epoch, manifest, tuple(sorted(nodes))
    except (EvidenceContractError, KeyError, TypeError):
        return None


def _scenario(evidence: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    scenarios = evidence.get("scenarios")
    if not isinstance(scenarios, Mapping):
        return {}
    value = scenarios.get(name)
    return value if isinstance(value, Mapping) else {}


def _same_digest(scenario: Mapping[str, Any]) -> bool:
    return (
        scenario.get("completed") is True
        and scenario.get("off_result_digest") == scenario.get("auto_result_digest")
        and isinstance(scenario.get("off_result_digest"), str)
        and _SHA256.fullmatch(str(scenario["off_result_digest"])) is not None
    )


def _supported_attempt_proof(
    document: Mapping[str, Any],
    supported_semantic: list[dict[str, object]],
    workers: set[str],
) -> tuple[dict[str, set[str]], bool, set[str]]:
    """Validate the complete Ray State candidate set without guessing ownership."""

    reasons: set[str] = set()
    partition_attempts: dict[str, set[str]] = defaultdict(set)
    structurally_complete = True
    if set(document) != {
        "schema_version",
        "semantic_event_count",
        "uncovered_event_count",
        "records",
    }:
        structurally_complete = False
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or type(document.get("semantic_event_count")) is not int
        or document.get("semantic_event_count") != len(supported_semantic)
        or not supported_semantic
        or type(document.get("uncovered_event_count")) is not int
        or document.get("uncovered_event_count") != 0
    ):
        structurally_complete = False

    records_raw = document.get("records")
    records = records_raw if isinstance(records_raw, list) else []
    if len(records) < 2:
        structurally_complete = False

    process_identities: set[tuple[str, str, int]] = set()
    canonical_by_task: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _TASK_STATE_FIELDS:
            structurally_complete = False
            continue
        try:
            task_id = _safe_id(record["task_id"])
            _safe_id(record["job_id"])
            actor_id = _safe_id(record["actor_id"])
            node_id = _safe_id(record["node_id"])
            _safe_id(record["worker_id"])
            parent_task_id = _safe_id(record["parent_task_id"])
            attempt_number = record["attempt_number"]
            worker_pid = record["worker_pid"]
            start_time_ms = record["start_time_ms"]
            end_time_ms = record["end_time_ms"]
            name = record["name"]
            if (
                type(attempt_number) is not int
                or type(worker_pid) is not int
                or type(start_time_ms) is not int
                or type(end_time_ms) is not int
                or worker_pid <= 0
                or start_time_ms <= 0
                or end_time_ms < start_time_ms
                or record["state"] != "FINISHED"
                or record["type"] != "ACTOR_TASK"
                or node_id not in workers
                or not isinstance(name, str)
                or not name.startswith("PhysicalScan->UDFProject")
            ):
                raise EvidenceContractError("task_state_record_invalid")
        except (EvidenceContractError, KeyError, TypeError):
            structurally_complete = False
            continue

        canonical = tuple(record[field] for field in sorted(_TASK_STATE_FIELDS))
        canonical_by_task[task_id].add(canonical)
        partition_attempts[task_id].add(f"attempt-{attempt_number}")
        process_identities.add((node_id, actor_id, worker_pid))

    if any(len(values) != 1 for values in canonical_by_task.values()):
        reasons.add("partition_attempt_not_unique")
    if any(
        attempt != "attempt-0"
        for attempts in partition_attempts.values()
        for attempt in attempts
    ):
        reasons.add("partition_task_retry_observed")

    semantic_processes = {
        (str(event["node_id"]), str(event["actor_id"]), int(event["pid"]))
        for event in supported_semantic
    }
    if (
        len(partition_attempts) < 2
        or not semantic_processes
        or not semantic_processes.issubset(process_identities)
        or len(canonical_by_task) != len(partition_attempts)
    ):
        structurally_complete = False
    if not structurally_complete:
        reasons.add("partition_attempt_attribution_incomplete")
    return dict(partition_attempts), structurally_complete, reasons


def aggregate_run_evidence(
    evidence: Mapping[str, Any], events: Iterable[Mapping[str, Any]]
) -> dict[str, object]:
    """Apply AE1-AE8 as evidence gates, never as timing/path inference."""

    reasons: set[str] = set()
    fail_reasons: set[str] = set()
    inconclusive_reasons: set[str] = set()
    stop_reasons: set[str] = set()
    checks = {
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
    }

    run_id = str(evidence.get("run_id", ""))
    cluster_epoch = str(evidence.get("cluster_epoch", ""))
    manifest = _manifest(evidence.get("manifest"))
    if not run_id or not cluster_epoch or manifest is None:
        checks["manifest"] = "fail"
        fail_reasons.add("required_manifest_missing")

    snapshots_raw = evidence.get("phase_snapshots")
    snapshots = (
        [_snapshot_identity(item) for item in snapshots_raw]
        if isinstance(snapshots_raw, list)
        else []
    )
    if (
        len(snapshots) != 3
        or any(item is None for item in snapshots)
        or {item[0] for item in snapshots if item is not None} != _PHASES
    ):
        checks["evidence_identity"] = "fail"
        fail_reasons.add("required_phase_evidence_missing")
    else:
        bases = {(item[1], item[2], item[3]) for item in snapshots if item is not None}
        if len(bases) != 1 or any(
            item[1] != cluster_epoch for item in snapshots if item is not None
        ):
            checks["evidence_identity"] = "inconclusive"
            inconclusive_reasons.add("phase_identity_drift")

    topology = evidence.get("topology")
    if isinstance(topology, Mapping):
        head = str(topology.get("head_node_id", ""))
        worker_values = topology.get("worker_node_ids", [])
        workers = {str(value) for value in worker_values} if isinstance(worker_values, list) else set()
    else:
        head, workers = "", set()
    if not head or len(workers) != 2 or head in workers:
        checks["readiness"] = "stop"
        stop_reasons.add("three_node_topology_invalid")

    readiness_raw = evidence.get("readiness")
    readiness = readiness_raw if isinstance(readiness_raw, list) else []
    ready_nodes = {
        str(item.get("node_id"))
        for item in readiness
        if isinstance(item, Mapping)
        and item.get("cinderx_compiled") is True
        and manifest is not None
        and item.get("manifest_sha256") == manifest["candidate_manifest_sha256"]
    }
    if ready_nodes != workers:
        checks["readiness"] = "stop"
        stop_reasons.add("worker_readiness_incomplete")

    qualification_raw = evidence.get("qualification")
    qualification = qualification_raw if isinstance(qualification_raw, list) else []
    qualified = {
        str(item.get("node_id")): item
        for item in qualification
        if isinstance(item, Mapping)
        and item.get("compiled") is True
        and item.get("result_digest")
        and item.get("process_generation")
    }
    qualification_values = list(qualified.values())
    qualification_consistent = bool(qualification_values) and all(
        _SHA256.fullmatch(str(item.get("artifact_hash", ""))) is not None
        and _SHA256.fullmatch(str(item.get("carrier_config_hash", ""))) is not None
        and _SHA256.fullmatch(str(item.get("result_digest", ""))) is not None
        and item.get("artifact_hash") == qualification_values[0].get("artifact_hash")
        and item.get("carrier_kind") == "RaySwordfishActor"
        and item.get("carrier_config_hash")
        == qualification_values[0].get("carrier_config_hash")
        and item.get("result_digest") == qualification_values[0].get("result_digest")
        for item in qualification_values
    )
    distinct_qualification_generations = {
        str(item.get("process_generation", "")) for item in qualification_values
    }
    if (
        set(qualified) != workers
        or not qualification_consistent
        or len(distinct_qualification_generations) != 2
    ):
        checks["worker_pool_qualification"] = "stop"
        stop_reasons.add("worker_pool_qualification_incomplete")

    safe_events: list[dict[str, object]] = []
    try:
        safe_events = [sanitize_event(event) for event in events]
    except EvidenceContractError:
        checks["evidence_identity"] = "fail"
        fail_reasons.add("event_schema_invalid")
    safe_events.sort(key=lambda item: int(item["timestamp_ns"]))
    if any(
        event["run_id"] != run_id or event["cluster_epoch"] != cluster_epoch
        for event in safe_events
    ):
        checks["evidence_identity"] = "inconclusive"
        inconclusive_reasons.add("event_identity_drift")

    data_plane = [
        event
        for event in safe_events
        if event["phase"] == "e2e"
        and event["stage"] in {"artifact", "layout", "jit", "execute"}
    ]
    if any(event["node_id"] == head for event in data_plane):
        checks["data_plane_isolation"] = "fail"
        fail_reasons.add("head_data_plane_event")
    if any(event["node_id"] not in workers for event in data_plane):
        checks["data_plane_isolation"] = "fail"
        fail_reasons.add("unknown_data_plane_node")

    supported = _scenario(evidence, "supported")
    supported_semantic = [
        event
        for event in safe_events
        if event["phase"] == "e2e"
        and event["scenario"] == "supported"
        and event["decision"] == "semantic_execute"
    ]
    participating_workers = {str(event["node_id"]) for event in supported_semantic}
    supported_runtime = [
        event
        for event in safe_events
        if event["phase"] == "e2e" and event["scenario"] == "supported"
    ]
    process_groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for event in supported_runtime:
        if event["decision"] in {"compile", "hit", "semantic_execute"}:
            key = (
                event["node_id"],
                event["actor_id"],
                event["pid"],
                event["process_generation"],
                event["variant_key"],
            )
            process_groups[key].append(event)
    chain_valid = bool(process_groups) and any(
        event["decision"] == "hit" for event in supported_runtime
    )
    for group in process_groups.values():
        decisions = [str(event["decision"]) for event in group]
        if "semantic_execute" in decisions:
            chain_valid = chain_valid and "compile" in decisions
            chain_valid = chain_valid and decisions.index("compile") < decisions.index(
                "semantic_execute"
            )
        if "hit" in decisions:
            chain_valid = chain_valid and "compile" in decisions
            chain_valid = chain_valid and decisions.index("compile") < decisions.index("hit")
    if (
        not _same_digest(supported)
        or int(supported.get("row_count", 0)) <= 0
        or supported.get("callable_calls") != 0
        or not participating_workers
        or not chain_valid
    ):
        checks["supported_hit"] = "fail"
        fail_reasons.add("compile_hit_chain_invalid")

    attempt_evidence = evidence.get("supported_attempt_evidence")
    if isinstance(attempt_evidence, Mapping):
        (
            partition_attempts,
            attribution_complete,
            attempt_reasons,
        ) = _supported_attempt_proof(
            attempt_evidence,
            supported_semantic,
            workers,
        )
        inconclusive_reasons.update(attempt_reasons)
    else:
        partition_attempts = defaultdict(set)
        attribution_complete = bool(supported_semantic)
        for event in supported_semantic:
            partition = str(event["partition_id"])
            attempt = str(event["task_attempt"])
            if not partition or not attempt:
                attribution_complete = False
                continue
            partition_attempts[partition].add(attempt)
        if any(len(attempts) != 1 for attempts in partition_attempts.values()):
            inconclusive_reasons.add("partition_attempt_not_unique")
        if any(
            attempt != "attempt-0"
            for attempts in partition_attempts.values()
            for attempt in attempts
        ):
            inconclusive_reasons.add("partition_task_retry_observed")
        if not attribution_complete or len(partition_attempts) < 2:
            inconclusive_reasons.add("partition_attempt_attribution_incomplete")
    if inconclusive_reasons & {
        "partition_attempt_not_unique",
        "partition_task_retry_observed",
        "partition_attempt_attribution_incomplete",
    }:
        checks["attempt_attribution"] = "inconclusive"

    guard = _scenario(evidence, "guard_miss")
    if not (
        _same_digest(guard)
        and guard.get("reason_code") == "schema_mismatch"
        and guard.get("semantic_execute_count") == 0
        and guard.get("row_count") == guard.get("off_callable_calls")
        and guard.get("off_callable_calls") == guard.get("auto_callable_calls")
        and guard.get("auto_callable_calls") == guard.get("fallback_count")
        and guard.get("fallback_count") == guard.get("side_effect_count")
    ):
        checks["guard_miss"] = "fail"
        fail_reasons.add("guard_miss_semantics_invalid")

    unsupported = _scenario(evidence, "unsupported")
    if not (
        _same_digest(unsupported)
        and unsupported.get("reason_code")
        in {"unsupported_opcode", "unsupported_dependency", "opaque_call"}
        and unsupported.get("row_count") == unsupported.get("off_callable_calls")
        and unsupported.get("off_callable_calls") == unsupported.get("auto_callable_calls")
        and unsupported.get("auto_callable_calls") == unsupported.get("side_effect_count")
    ):
        checks["unsupported"] = "fail"
        fail_reasons.add("unsupported_semantics_invalid")

    fail_open_expectations = {
        "mode_off": "mode_off",
        "fingerprint_mismatch": "with_columns_fingerprint_mismatch",
        "corrupt_artifact": "artifact_mismatch",
    }
    if any(
        _scenario(evidence, name).get("completed") is not True
        or _scenario(evidence, name).get("reason_code") != reason
        or _SHA256.fullmatch(str(_scenario(evidence, name).get("result_digest", "")))
        is None
        or _scenario(evidence, name).get("result_digest")
        != _scenario(evidence, name).get("expected_result_digest")
        for name, reason in fail_open_expectations.items()
    ):
        checks["fail_open"] = "fail"
        fail_reasons.add("fail_open_scenario_invalid")

    zero = _scenario(evidence, "zero_row")
    if not (
        _same_digest(zero)
        and zero.get("row_count") == 0
        and zero.get("callable_calls") == 0
        and zero.get("descriptor_count") == 0
        and zero.get("compile_count") == 0
        and zero.get("hit_count") == 0
        and zero.get("activity_event_count") == 0
    ):
        checks["zero_row"] = "fail"
        fail_reasons.add("zero_row_semantics_invalid")

    reasons.update(stop_reasons)
    reasons.update(inconclusive_reasons)
    reasons.update(fail_reasons)
    if stop_reasons:
        verdict = "stop"
    elif fail_reasons:
        verdict = "fail"
    elif inconclusive_reasons:
        verdict = "inconclusive"
    else:
        verdict = "pass"

    process_summaries = []
    for key, group in sorted(process_groups.items(), key=lambda item: tuple(map(str, item[0]))):
        process_summaries.append(
            {
                "node_id": key[0],
                "actor_id": key[1],
                "pid": key[2],
                "process_generation": key[3],
                "variant_key": key[4],
                "artifact_hash": next(
                    (event["artifact_hash"] for event in group if event["artifact_hash"]),
                    "",
                ),
                "code_hash": next(
                    (event["code_hash"] for event in group if event["code_hash"]),
                    "",
                ),
                "compile_count": sum(event["decision"] == "compile" for event in group),
                "hit_count": sum(event["decision"] == "hit" for event in group),
                "semantic_execute_count": sum(
                    event["decision"] == "semantic_execute" for event in group
                ),
            }
        )
    fixed_topology = []
    if snapshots and snapshots[0] is not None:
        fixed_topology = [
            {
                "role": role,
                "node_id": node_id,
                "container_boot_id": container_boot_id,
            }
            for role, node_id, container_boot_id in snapshots[0][3]
        ]
    result_summaries = {}
    for name in sorted(_SCENARIOS):
        scenario = _scenario(evidence, name)
        result_summaries[name] = {
            "completed": scenario.get("completed") is True,
            "row_count": int(scenario.get("row_count", 0)),
            "off_auto_equivalent": (
                scenario.get("off_result_digest") == scenario.get("auto_result_digest")
                if "off_result_digest" in scenario
                else scenario.get("result_digest")
                == scenario.get("expected_result_digest")
            ),
        }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "verdict": verdict,
        "reason_codes": sorted(reasons),
        "manifest": manifest or {},
        "fixed_topology": fixed_topology,
        "head_node_id": head,
        "ready_worker_node_ids": sorted(ready_nodes),
        "qualified_worker_node_ids": sorted(set(qualified) & workers),
        "qualified_worker_coverage": f"{len(set(qualified) & workers)}/2",
        "participating_worker_node_ids": sorted(participating_workers),
        "natural_worker_coverage": f"{len(participating_workers)}/2",
        "remote_partition_task_count": len(partition_attempts) if attribution_complete else 0,
        "participating_processes": process_summaries,
        "checks": checks,
        "event_counts": {
            "compile": sum(event["decision"] == "compile" for event in safe_events),
            "hit": sum(event["decision"] == "hit" for event in safe_events),
            "semantic_execute": sum(
                event["decision"] == "semantic_execute" for event in safe_events
            ),
            "fallback": sum(event["decision"] == "fallback" for event in safe_events),
        },
        "result_summaries": result_summaries,
    }


class EvidenceRun:
    """0600 raw JSONL in a per-Run 0700 directory, deleted after aggregation."""

    def __init__(self, root: str | Path, run_id: str) -> None:
        self.run_id = _safe_id(run_id)
        root_path = Path(root)
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.raw_dir = root_path / self.run_id
        self.raw_dir.mkdir(mode=0o700, exist_ok=False)
        self.raw_file = self.raw_dir / "events.jsonl"
        descriptor = os.open(
            self.raw_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)

    def append_event(self, event: Mapping[str, Any]) -> None:
        safe = sanitize_event(event)
        descriptor = os.open(self.raw_file, os.O_WRONLY | os.O_APPEND)
        try:
            with os.fdopen(descriptor, "a", encoding="ascii") as stream:
                stream.write(
                    json.dumps(
                        safe,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                stream.write("\n")
        except Exception:
            os.close(descriptor)
            raise

    def _read_events(self) -> list[dict[str, object]]:
        events = []
        with self.raw_file.open("r", encoding="ascii") as stream:
            for line in stream:
                if line.strip():
                    events.append(json.loads(line))
        return events

    def finalize(
        self, evidence: Mapping[str, Any], output_path: str | Path
    ) -> dict[str, object]:
        output = Path(output_path)
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            report = aggregate_run_evidence(evidence, self._read_events())
            payload = json.dumps(
                report, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            return report
        finally:
            if self.raw_dir.exists():
                shutil.rmtree(self.raw_dir)
