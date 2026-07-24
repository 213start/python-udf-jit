from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOCKER_BRIDGE = re.compile(r"^br-[0-9a-f]{12}$")
_WORKER_ROLES = ("ray-worker-1", "ray-worker-2")


def _proof_hash(document: Mapping[str, Any]) -> str:
    material = {
        key: value
        for key, value in document.items()
        if key != "proof_sha256"
    }
    payload = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _identity(
    proof: Mapping[str, Any], *, run_id: str, cluster_epoch: str
) -> bool:
    return (
        proof.get("schema_version") == 1
        and proof.get("status") == "pass"
        and proof.get("run_id") == run_id
        and proof.get("cluster_epoch") == cluster_epoch
        and isinstance(proof.get("proof_sha256"), str)
        and _SHA256.fullmatch(str(proof["proof_sha256"])) is not None
        and proof["proof_sha256"] == _proof_hash(proof)
    )


def seal_environment_proof(document: Mapping[str, Any]) -> dict[str, object]:
    """Return an immutable-by-hash proof document for an external collector."""

    if "proof_sha256" in document:
        raise ValueError("environment proof is already sealed")
    sealed = dict(document)
    sealed["proof_sha256"] = _proof_hash(sealed)
    return sealed


def validate_auth_evidence(
    proof: object, *, run_id: str, cluster_epoch: str
) -> str:
    if not isinstance(proof, Mapping):
        return "incomplete"
    dashboard = proof.get("dashboard")
    if not isinstance(dashboard, Mapping):
        return "incomplete"
    required = (
        "published_bindings",
        "published_non_dashboard_ports",
        "non_loopback_connect",
        "requests",
        "token_file_mode",
    )
    if any(field not in dashboard for field in required):
        return "incomplete"
    requests = dashboard["requests"]
    if not isinstance(requests, Mapping):
        return "fail"
    expected_binding = [
        {
            "host_ip": "127.0.0.1",
            "host_port": 8265,
            "container_port": 8265,
            "protocol": "tcp",
        }
    ]
    valid = (
        _identity(proof, run_id=run_id, cluster_epoch=cluster_epoch)
        and dashboard["published_bindings"] == expected_binding
        and dashboard["published_non_dashboard_ports"] == []
        and dashboard["non_loopback_connect"] == "refused"
        and dashboard["token_file_mode"] == "0600"
        and requests.get("unauthenticated") == 401
        and requests.get("wrong_token") == 403
        and requests.get("authenticated") == 200
    )
    return "pass" if valid else "fail"


def validate_secret_evidence(
    proof: object, *, run_id: str, cluster_epoch: str
) -> str:
    if not isinstance(proof, Mapping):
        return "incomplete"
    scan = proof.get("secret_scan")
    if not isinstance(scan, Mapping):
        return "incomplete"
    required = (
        "scanned_artifact_count",
        "scanned_image_count",
        "token_matches",
        "token_in_image_environment",
        "token_in_image_history",
        "token_in_retained_reports",
    )
    if any(field not in scan for field in required):
        return "incomplete"
    valid = (
        _identity(proof, run_id=run_id, cluster_epoch=cluster_epoch)
        and isinstance(scan["scanned_artifact_count"], int)
        and not isinstance(scan["scanned_artifact_count"], bool)
        and scan["scanned_artifact_count"] >= 1
        and scan["scanned_image_count"] == 1
        and scan["token_matches"] == 0
        and scan["token_in_image_environment"] is False
        and scan["token_in_image_history"] is False
        and scan["token_in_retained_reports"] is False
    )
    return "pass" if valid else "fail"


def validate_cleanup_evidence(
    proof: object, *, run_id: str, cluster_epoch: str
) -> str:
    if not isinstance(proof, Mapping):
        return "incomplete"
    before = proof.get("before")
    after = proof.get("after")
    cleanup = proof.get("cleanup")
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or not isinstance(cleanup, Mapping)
    ):
        return "incomplete"
    digest_fields = (
        "routes_sha256",
        "firewall_sha256",
        "firewalld_runtime_sha256",
        "firewalld_permanent_sha256",
    )
    metadata_fields = ("firewall_backend", "firewalld_state")
    state_fields = (*digest_fields, *metadata_fields)
    cleanup_fields = (
        "removed_container_ids",
        "removed_network_ids",
        "remaining_project_containers",
        "remaining_project_networks",
        "dashboard_port_open",
        "token_exists",
        "bridge_accommodation",
    )
    if (
        any(field not in before or field not in after for field in state_fields)
        or any(field not in cleanup for field in cleanup_fields)
    ):
        return "incomplete"
    digests_valid = all(
        isinstance(state[field], str)
        and _SHA256.fullmatch(str(state[field])) is not None
        for state in (before, after)
        for field in digest_fields
    )
    bridge = cleanup.get("bridge_accommodation")
    bridge_complete = isinstance(bridge, Mapping) and all(
        field in bridge
        for field in (
            "action",
            "network_id",
            "bridge_interface",
            "zone",
            "scope",
            "connectivity_before",
            "connectivity_after",
            "binding_added",
            "binding_removed",
            "bridge_interface_exists_after_cleanup",
        )
    )
    bridge_valid = False
    if bridge_complete and isinstance(bridge, Mapping):
        network_id = bridge["network_id"]
        interface = bridge["bridge_interface"]
        before_connectivity = bridge["connectivity_before"]
        after_connectivity = bridge["connectivity_after"]
        common_valid = (
            isinstance(network_id, str)
            and _SHA256.fullmatch(network_id) is not None
            and isinstance(interface, str)
            and _DOCKER_BRIDGE.fullmatch(interface) is not None
            and interface == f"br-{network_id[:12]}"
            and isinstance(before_connectivity, Mapping)
            and set(before_connectivity) == set(_WORKER_ROLES)
            and all(
                isinstance(before_connectivity[role], bool)
                for role in _WORKER_ROLES
            )
            and isinstance(after_connectivity, Mapping)
            and set(after_connectivity) == set(_WORKER_ROLES)
            and all(
                after_connectivity[role] is True for role in _WORKER_ROLES
            )
            and bridge["bridge_interface_exists_after_cleanup"] is False
        )
        if bridge["action"] == "runtime-trusted":
            bridge_valid = (
                common_valid
                and not all(
                    before_connectivity[role] for role in _WORKER_ROLES
                )
                and bridge["zone"] == "trusted"
                and bridge["scope"] == "runtime"
                and bridge["binding_added"] is True
                and bridge["binding_removed"] is True
            )
        elif bridge["action"] == "not-required":
            bridge_valid = (
                common_valid
                and all(
                    before_connectivity[role] for role in _WORKER_ROLES
                )
                and bridge["zone"] is None
                and bridge["scope"] is None
                and bridge["binding_added"] is False
                and bridge["binding_removed"] is False
            )
    valid = (
        _identity(proof, run_id=run_id, cluster_epoch=cluster_epoch)
        and digests_valid
        and before["firewall_backend"] == "nftables-stateless"
        and after["firewall_backend"] == "nftables-stateless"
        and before["firewalld_state"] == "running"
        and after["firewalld_state"] == "running"
        and all(before[field] == after[field] for field in state_fields)
        and isinstance(cleanup["removed_container_ids"], list)
        and len(cleanup["removed_container_ids"]) == 3
        and len(set(cleanup["removed_container_ids"])) == 3
        and all(
            isinstance(identifier, str) and len(identifier) >= 12
            for identifier in cleanup["removed_container_ids"]
        )
        and isinstance(cleanup["removed_network_ids"], list)
        and len(cleanup["removed_network_ids"]) == 2
        and len(set(cleanup["removed_network_ids"])) == 2
        and all(
            isinstance(identifier, str) and len(identifier) >= 12
            for identifier in cleanup["removed_network_ids"]
        )
        and cleanup["remaining_project_containers"] == []
        and cleanup["remaining_project_networks"] == []
        and cleanup["dashboard_port_open"] is False
        and cleanup["token_exists"] is False
        and isinstance(bridge, Mapping)
        and bridge.get("network_id") in cleanup["removed_network_ids"]
        and bridge_valid
    )
    return "pass" if valid else "fail"


def validate_hygiene_evidence(
    proof: object, *, run_id: str, cluster_epoch: str
) -> str:
    if not isinstance(proof, Mapping):
        return "incomplete"
    hygiene = proof.get("evidence_hygiene")
    if not isinstance(hygiene, Mapping):
        return "incomplete"
    required = (
        "retained_reports",
        "raw_event_files_remaining",
        "raw_event_files_removed",
    )
    if any(field not in hygiene for field in required):
        return "incomplete"
    reports = hygiene["retained_reports"]
    removed = hygiene["raw_event_files_removed"]
    if not isinstance(reports, list) or not isinstance(removed, list):
        return "fail"
    valid_reports = (
        len(reports) >= 1
        and all(
            isinstance(report, Mapping)
            and isinstance(report.get("name"), str)
            and bool(report["name"])
            and report.get("mode") == "0600"
            and isinstance(report.get("sha256"), str)
            and _SHA256.fullmatch(str(report["sha256"])) is not None
            for report in reports
        )
        and len({str(report["name"]) for report in reports}) == len(reports)
    )
    valid = (
        _identity(proof, run_id=run_id, cluster_epoch=cluster_epoch)
        and valid_reports
        and hygiene["raw_event_files_remaining"] == []
        and len(removed) >= 1
        and all(isinstance(name, str) and bool(name) for name in removed)
        and len(set(removed)) == len(removed)
    )
    return "pass" if valid else "fail"
