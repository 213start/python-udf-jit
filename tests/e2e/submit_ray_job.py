from __future__ import annotations

import argparse
import json
import ssl
import stat
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})


def _request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    document: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = None
    headers = {"Authorization": f"Bearer {token}"}
    if document is not None:
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=payload, headers=headers, method=method
    )
    with urllib.request.urlopen(
        request, timeout=10, context=ssl.create_default_context()
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def submit_and_wait(
    *,
    address: str,
    token_file: Path,
    submission_id: str,
    entrypoint: str,
    mode: str,
    timeout_seconds: int,
) -> dict[str, object]:
    parsed = urlsplit(address)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Ray Jobs address must be an HTTP loopback endpoint")
    token_mode = stat.S_IMODE(token_file.stat().st_mode)
    if token_mode & 0o077:
        raise PermissionError("Ray authentication token must not be group/world accessible")
    token = token_file.read_text(encoding="ascii").strip()
    if not token:
        raise RuntimeError("empty Ray authentication token")
    base = address.rstrip("/")
    submitted = _request(
        f"{base}/api/jobs/",
        token,
        method="POST",
        document={
            "entrypoint": entrypoint,
            "submission_id": submission_id,
            "runtime_env": {"env_vars": {"UDFJIT_MODE": mode}},
        },
    )
    started = time.monotonic()
    while True:
        status = _request(f"{base}/api/jobs/{submission_id}", token)
        state = str(status.get("status", ""))
        if state in _TERMINAL:
            if state != "SUCCEEDED":
                raise RuntimeError(f"Ray Job ended with status {state}")
            return {
                "submission_id": submission_id,
                "status": state,
                "job_id": submitted.get("job_id") or status.get("job_id") or "",
            }
        if time.monotonic() - started >= timeout_seconds:
            raise TimeoutError("Ray Job did not reach a terminal state before timeout")
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="http://127.0.0.1:8265")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--mode", choices=("off", "auto"), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    arguments = parser.parse_args()
    result = submit_and_wait(
        address=arguments.address,
        token_file=arguments.token_file,
        submission_id=arguments.submission_id,
        entrypoint=arguments.entrypoint,
        mode=arguments.mode,
        timeout_seconds=arguments.timeout_seconds,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
