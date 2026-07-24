from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
)
from tests.system.capture_host_state import capture, write_output


def _private_document(path: Path) -> Mapping[str, Any]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"{path} must be mode 0600")
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, Mapping):
        raise ValueError(f"{path} must contain one JSON object")
    return document


def _docker_ids(kind: str, project: str) -> list[str]:
    if kind == "container":
        arguments = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    elif kind == "network":
        arguments = [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    else:
        raise ValueError(f"unsupported Docker resource kind: {kind}")
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return sorted(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    )


def _port_open(host: str, port: int) -> bool:
    try:
        connection = socket.create_connection((host, port), timeout=2)
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False
    connection.close()
    return True


def build_cleanup_proof(
    *,
    run_id: str,
    cluster_epoch: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    removed_container_ids: list[str],
    removed_network_ids: list[str],
    remaining_project_containers: list[str],
    remaining_project_networks: list[str],
    dashboard_port_open: bool,
    token_exists: bool,
    bridge_accommodation: Mapping[str, Any],
) -> dict[str, object]:
    state_fields = (
        "routes_sha256",
        "firewall_sha256",
        "firewalld_runtime_sha256",
        "firewalld_permanent_sha256",
        "firewall_backend",
        "firewalld_state",
    )
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "before": {
                field: before.get(field) for field in state_fields
            },
            "after": {
                field: after.get(field) for field in state_fields
            },
            "cleanup": {
                "removed_container_ids": sorted(removed_container_ids),
                "removed_network_ids": sorted(removed_network_ids),
                "remaining_project_containers": sorted(
                    remaining_project_containers
                ),
                "remaining_project_networks": sorted(
                    remaining_project_networks
                ),
                "dashboard_port_open": dashboard_port_open,
                "token_exists": token_exists,
                "bridge_accommodation": dict(bridge_accommodation),
            },
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--removed-container-id", action="append", required=True)
    parser.add_argument("--removed-network-id", action="append", required=True)
    parser.add_argument("--bridge-accommodation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    before = _private_document(arguments.before)
    bridge_accommodation = _private_document(
        arguments.bridge_accommodation
    )
    after = capture()
    proof = build_cleanup_proof(
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        before=before,
        after=after,
        removed_container_ids=arguments.removed_container_id,
        removed_network_ids=arguments.removed_network_id,
        remaining_project_containers=_docker_ids(
            "container", arguments.project
        ),
        remaining_project_networks=_docker_ids(
            "network", arguments.project
        ),
        dashboard_port_open=_port_open("127.0.0.1", 8265),
        token_exists=arguments.token_file.exists(),
        bridge_accommodation=bridge_accommodation,
    )
    write_output(arguments.output, proof)
    print(proof["proof_sha256"])


if __name__ == "__main__":
    main()
