from __future__ import annotations

import argparse
import json
import socket
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
)
from tests.system.loopback_http import loopback_urlopen
from tests.system.private_output import write_private_json


def _run_json(arguments: list[str]) -> object:
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def _published_ports(
    documents: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(documents, list) or len(documents) != 3:
        raise RuntimeError("Docker inspect must resolve exactly three containers")
    dashboard: list[dict[str, object]] = []
    others: list[dict[str, object]] = []
    for document in documents:
        if not isinstance(document, dict):
            raise RuntimeError("Docker inspect entry is not an object")
        ports = document.get("NetworkSettings", {}).get("Ports", {})
        if not isinstance(ports, dict):
            raise RuntimeError("Docker port observation is malformed")
        for raw_container_port, raw_bindings in ports.items():
            if raw_bindings is None:
                continue
            container_text, protocol = str(raw_container_port).split("/", 1)
            for raw_binding in raw_bindings:
                binding = {
                    "host_ip": str(raw_binding["HostIp"]),
                    "host_port": int(raw_binding["HostPort"]),
                    "container_port": int(container_text),
                    "protocol": protocol,
                }
                if binding["container_port"] == 8265:
                    dashboard.append(binding)
                else:
                    others.append(binding)
    key = lambda item: (
        str(item["host_ip"]),
        int(item["host_port"]),
        int(item["container_port"]),
        str(item["protocol"]),
    )
    return sorted(dashboard, key=key), sorted(others, key=key)


def _http_status(url: str, token: str | None) -> int:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with loopback_urlopen(request, timeout=10) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as error:
        error.read()
        return int(error.code)


def _non_loopback_connect(host: str, port: int) -> str:
    try:
        connection = socket.create_connection((host, port), timeout=3)
    except ConnectionRefusedError:
        return "refused"
    except (TimeoutError, OSError) as error:
        raise RuntimeError(
            f"non-loopback dashboard probe did not fail closed: {error}"
        ) from error
    else:
        connection.close()
        return "connected"


def _scan_files(paths: Iterable[Path], token: bytes) -> tuple[int, int, bool]:
    count = 0
    matches = 0
    report_match = False
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"secret scan input is not a file: {path}")
        payload = path.read_bytes()
        count += 1
        occurrences = payload.count(token)
        matches += occurrences
        if occurrences and path.suffix == ".json":
            report_match = True
    return count, matches, report_match


def _write(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def probe(
    *,
    run_id: str,
    cluster_epoch: str,
    address: str,
    non_loopback_host: str,
    token_file: Path,
    containers: list[str],
    image: str,
    scan_artifacts: list[Path],
) -> dict[str, object]:
    token_mode = stat.S_IMODE(token_file.stat().st_mode)
    if token_mode != 0o600:
        raise PermissionError("Ray token file must be mode 0600")
    token = token_file.read_text(encoding="ascii").strip()
    if not token:
        raise RuntimeError("Ray token is empty")
    if len(containers) != 3 or len(set(containers)) != 3:
        raise ValueError("exactly three distinct containers are required")

    container_documents = _run_json(["docker", "inspect", *containers])
    dashboard, other_ports = _published_ports(container_documents)
    endpoint = f"{address.rstrip('/')}/api/jobs/"
    requests = {
        "unauthenticated": _http_status(endpoint, None),
        "wrong_token": _http_status(
            endpoint,
            "udfjit-intentionally-wrong-token",
        ),
        "authenticated": _http_status(endpoint, token),
    }

    image_documents = _run_json(["docker", "image", "inspect", image])
    history = subprocess.run(
        [
            "docker",
            "history",
            "--no-trunc",
            "--format",
            "{{json .}}",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    token_bytes = token.encode("ascii")
    image_text = json.dumps(
        image_documents,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact_count, matches, report_match = _scan_files(
        scan_artifacts, token_bytes
    )
    image_environment_match = token_bytes in image_text
    image_history_match = token in history
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "dashboard": {
                "published_bindings": dashboard,
                "published_non_dashboard_ports": other_ports,
                "non_loopback_connect": _non_loopback_connect(
                    non_loopback_host, 8265
                ),
                "requests": requests,
                "token_file_mode": f"{token_mode:04o}",
            },
            "secret_scan": {
                "scanned_artifact_count": artifact_count,
                "scanned_image_count": len(image_documents),
                "token_matches": (
                    matches
                    + int(image_environment_match)
                    + int(image_history_match)
                ),
                "token_in_image_environment": image_environment_match,
                "token_in_image_history": image_history_match,
                "token_in_retained_reports": report_match,
            },
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--address", default="http://127.0.0.1:8265")
    parser.add_argument("--non-loopback-host", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--container", action="append", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--scan-artifact", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = probe(
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        address=arguments.address,
        non_loopback_host=arguments.non_loopback_host,
        token_file=arguments.token_file,
        containers=arguments.container,
        image=arguments.image,
        scan_artifacts=arguments.scan_artifact,
    )
    _write(arguments.output, document)
    print(document["proof_sha256"])


if __name__ == "__main__":
    main()
