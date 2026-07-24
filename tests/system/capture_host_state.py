from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


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
    iptables_save = shutil.which("iptables-save")
    if ip is None or iptables_save is None:
        raise RuntimeError("ip and iptables-save are required for host-state proof")
    routes = _canonical_routes(
        _run([ip, "-j", "-4", "route", "show", "table", "main"])
    )
    firewall = _run([iptables_save])
    return {
        "schema_version": 1,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "firewall_sha256": hashlib.sha256(firewall).hexdigest(),
    }


def write_output(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


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
