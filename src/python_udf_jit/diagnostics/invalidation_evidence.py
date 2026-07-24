from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ROLES = ("ray-head-driver", "ray-worker-1", "ray-worker-2")


class InvalidationEvidenceError(ValueError):
    """A real phase-drift probe did not invalidate evidence as required."""


def _private_document(path: Path, field: str) -> tuple[Mapping[str, Any], str]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        payload = path.read_bytes()
    except OSError as error:
        raise InvalidationEvidenceError(f"{field}_unreadable") from error
    if not path.is_file() or mode != 0o600:
        raise InvalidationEvidenceError(f"{field}_mode_invalid")
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidationEvidenceError(f"{field}_json_invalid") from error
    if not isinstance(document, Mapping):
        raise InvalidationEvidenceError(f"{field}_shape_invalid")
    return document, hashlib.sha256(payload).hexdigest()


def _nodes(
    snapshot: Mapping[str, Any], field: str
) -> dict[str, Mapping[str, Any]]:
    raw_nodes = snapshot.get("nodes")
    if not isinstance(raw_nodes, list) or len(raw_nodes) != 3:
        raise InvalidationEvidenceError(f"{field}_nodes_invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            raise InvalidationEvidenceError(f"{field}_node_invalid")
        role = node.get("role")
        if role not in _ROLES or role in result:
            raise InvalidationEvidenceError(f"{field}_roles_invalid")
        if not all(
            isinstance(node.get(name), str) and bool(node[name])
            for name in ("node_id", "container_boot_id")
        ):
            raise InvalidationEvidenceError(f"{field}_node_identity_invalid")
        result[str(role)] = node
    if set(result) != set(_ROLES):
        raise InvalidationEvidenceError(f"{field}_roles_invalid")
    return result


def build_invalidation_evidence(
    *,
    source_git_commit: str,
    before_snapshot_path: Path,
    after_snapshot_path: Path,
    invalidated_report_path: Path,
) -> dict[str, object]:
    if _GIT_COMMIT.fullmatch(source_git_commit) is None:
        raise InvalidationEvidenceError("source_git_commit_invalid")
    before, before_digest = _private_document(before_snapshot_path, "before")
    after, after_digest = _private_document(after_snapshot_path, "after")
    report, report_digest = _private_document(
        invalidated_report_path, "invalidated_report"
    )
    identity = (
        before.get("run_id"),
        before.get("cluster_epoch"),
    )
    if (
        not all(isinstance(value, str) and value for value in identity)
        or (after.get("run_id"), after.get("cluster_epoch")) != identity
        or (report.get("run_id"), report.get("cluster_epoch")) != identity
        or before.get("phase") != "e2e"
        or after.get("phase") != "e2e"
        or before.get("manifest_sha256") != after.get("manifest_sha256")
        or before.get("image_digest") != after.get("image_digest")
        or not isinstance(before.get("manifest_sha256"), str)
        or _SHA256.fullmatch(str(before["manifest_sha256"])) is None
        or not isinstance(before.get("image_digest"), str)
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(before["image_digest"])
        )
        is None
    ):
        raise InvalidationEvidenceError("phase_identity_contract_invalid")

    before_nodes = _nodes(before, "before")
    after_nodes = _nodes(after, "after")
    for stable_role in ("ray-head-driver", "ray-worker-1"):
        if before_nodes[stable_role] != after_nodes[stable_role]:
            raise InvalidationEvidenceError(
                f"unexpected_stable_role_drift:{stable_role}"
            )
    old_worker = before_nodes["ray-worker-2"]
    new_worker = after_nodes["ray-worker-2"]
    if old_worker["container_boot_id"] == new_worker["container_boot_id"]:
        raise InvalidationEvidenceError("worker_restart_not_observed")
    reason_codes = report.get("reason_codes")
    if (
        report.get("verdict") != "inconclusive"
        or not isinstance(reason_codes, list)
        or "phase_identity_drift" not in reason_codes
    ):
        raise InvalidationEvidenceError("invalidation_verdict_invalid")

    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": identity[0],
            "cluster_epoch": identity[1],
            "source_git_commit": source_git_commit,
            "probe": {
                "changed_role": "ray-worker-2",
                "before_boot_id": old_worker["container_boot_id"],
                "after_boot_id": new_worker["container_boot_id"],
                "head_unchanged": True,
                "other_worker_unchanged": True,
                "manifest_unchanged": True,
                "image_unchanged": True,
                "invalidated_verdict": "inconclusive",
                "reason_code": "phase_identity_drift",
            },
            "artifacts": {
                "before_snapshot_sha256": before_digest,
                "after_snapshot_sha256": after_digest,
                "invalidated_report_sha256": report_digest,
            },
        }
    )


def validate_invalidation_evidence(
    proof: object,
    *,
    run_id: str,
    cluster_epoch: str,
    source_git_commit: str,
) -> str:
    if not isinstance(proof, Mapping):
        return "incomplete"
    probe = proof.get("probe")
    artifacts = proof.get("artifacts")
    if not isinstance(probe, Mapping) or not isinstance(artifacts, Mapping):
        return "incomplete"
    expected_probe = {
        "changed_role": "ray-worker-2",
        "head_unchanged": True,
        "other_worker_unchanged": True,
        "manifest_unchanged": True,
        "image_unchanged": True,
        "invalidated_verdict": "inconclusive",
        "reason_code": "phase_identity_drift",
    }
    required_artifacts = (
        "before_snapshot_sha256",
        "after_snapshot_sha256",
        "invalidated_report_sha256",
    )
    if (
        any(field not in probe for field in (*expected_probe, "before_boot_id", "after_boot_id"))
        or any(field not in artifacts for field in required_artifacts)
    ):
        return "incomplete"
    unsealed = {
        key: value
        for key, value in proof.items()
        if key != "proof_sha256"
    }
    resealed = seal_environment_proof(unsealed)
    valid = (
        proof.get("schema_version") == 1
        and proof.get("status") == "pass"
        and proof.get("run_id") == run_id
        and proof.get("cluster_epoch") == cluster_epoch
        and proof.get("source_git_commit") == source_git_commit
        and all(probe.get(field) == value for field, value in expected_probe.items())
        and isinstance(probe["before_boot_id"], str)
        and isinstance(probe["after_boot_id"], str)
        and bool(probe["before_boot_id"])
        and bool(probe["after_boot_id"])
        and probe["before_boot_id"] != probe["after_boot_id"]
        and all(
            isinstance(artifacts[field], str)
            and _SHA256.fullmatch(str(artifacts[field])) is not None
            for field in required_artifacts
        )
        and proof.get("proof_sha256") == resealed["proof_sha256"]
    )
    return "pass" if valid else "fail"
