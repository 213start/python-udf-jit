from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from tests.system.private_output import write_private_json


def _run(arguments: list[str]) -> bytes:
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return completed.stdout


def _canonical_routes(payload: bytes) -> bytes:
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, list):
        raise RuntimeError("route command did not return a JSON list")
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def capture() -> dict[str, object]:
    ip = shutil.which("ip")
    nft = shutil.which("nft")
    firewall_cmd = shutil.which("firewall-cmd")
    if ip is None or nft is None or firewall_cmd is None:
        raise RuntimeError(
            "ip, nft, and firewall-cmd are required for host-state proof"
        )
    routes = _canonical_routes(
        _run([ip, "-j", "-4", "route", "show", "table", "main"])
    )
    firewalld_state = _run([firewall_cmd, "--state"]).decode(
        "ascii"
    ).strip()
    if firewalld_state != "running":
        raise RuntimeError("firewalld must be running for host-state proof")
    firewall = _run([nft, "--stateless", "list", "ruleset"])
    firewalld_runtime = _run([firewall_cmd, "--list-all-zones"])
    firewalld_permanent = _run(
        [firewall_cmd, "--permanent", "--list-all-zones"]
    )
    return {
        "schema_version": 2,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "firewall_sha256": hashlib.sha256(firewall).hexdigest(),
        "firewalld_runtime_sha256": hashlib.sha256(
            firewalld_runtime
        ).hexdigest(),
        "firewalld_permanent_sha256": hashlib.sha256(
            firewalld_permanent
        ).hexdigest(),
        "firewall_backend": "nftables-stateless",
        "firewalld_state": firewalld_state,
    }


def write_output(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = capture()
    write_output(arguments.output, document)
    print(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
