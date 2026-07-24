from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


_ROLES = ("ray-head-driver", "ray-worker-1", "ray-worker-2")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _run(arguments: list[str], *, timeout: int = 30) -> str:
    completed = subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def _runtime(container: str) -> dict[str, str]:
    source = (
        "import hashlib,json,platform,sysconfig;"
        "from pathlib import Path;import daft,pyarrow,ray;"
        "p=Path('/opt/python-udf-jit/config/scalar-piercing-manifest.json');"
        "print(json.dumps({'python_version':platform.python_version(),"
        "'soabi':str(sysconfig.get_config_var('SOABI') or ''),"
        "'daft_version':daft.__version__,'ray_version':ray.__version__,"
        "'pyarrow_version':pyarrow.__version__,"
        "'candidate_manifest_sha256':hashlib.sha256(p.read_bytes()).hexdigest()},sort_keys=True))"
    )
    output = _run(["docker", "exec", container, "python", "-c", source], timeout=60)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"runtime manifest probe failed in {container}")
    return {str(key): str(value) for key, value in json.loads(lines[-1]).items()}


def capture(
    *,
    containers: dict[str, str],
    source_git_commit: str,
    cinderx_commit: str,
    cinderx_source_tree_sha256: str,
    cinderx_patch_sha256: str,
    udf_jit_wheel_sha256: str,
) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", source_git_commit) is None:
        raise ValueError("source_git_commit must be a full lowercase Git object ID")
    if re.fullmatch(r"[0-9a-f]{40}", cinderx_commit) is None:
        raise ValueError("cinderx_commit must be a full lowercase Git object ID")
    for field, value in (
        ("cinderx_source_tree_sha256", cinderx_source_tree_sha256),
        ("cinderx_patch_sha256", cinderx_patch_sha256),
        ("udf_jit_wheel_sha256", udf_jit_wheel_sha256),
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"{field} must be a lowercase SHA-256")
    runtimes = [_runtime(containers[role]) for role in _ROLES]
    if any(runtime != runtimes[0] for runtime in runtimes[1:]):
        raise RuntimeError("runtime/manifest drift across cluster roles")
    inspect_documents = [
        json.loads(_run(["docker", "inspect", containers[role]]))[0]
        for role in _ROLES
    ]
    image_digests = {str(document["Image"]) for document in inspect_documents}
    if len(image_digests) != 1:
        raise RuntimeError("candidate image drift across cluster roles")
    expected_labels = {
        "org.opencontainers.image.revision": source_git_commit,
        "org.python-udf-jit.cinderx-commit": cinderx_commit,
        "org.python-udf-jit.cinderx-source-tree-sha256": cinderx_source_tree_sha256,
        "org.python-udf-jit.cinderx-patch-sha256": cinderx_patch_sha256,
        "org.python-udf-jit.wheel-sha256": udf_jit_wheel_sha256,
    }
    for document in inspect_documents:
        labels = document.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict) or any(
            labels.get(name) != value for name, value in expected_labels.items()
        ):
            raise RuntimeError("candidate source/image label drift")
    runtime = runtimes[0]
    if (
        runtime["python_version"] != "3.14.3"
        or runtime["daft_version"] != "0.7.2"
        or runtime["ray_version"] != "2.55.0"
        or runtime["pyarrow_version"] != "22.0.0"
    ):
        raise RuntimeError("locked runtime version drift")
    return {
        "candidate_manifest_sha256": runtime["candidate_manifest_sha256"],
        "image_digest": next(iter(image_digests)),
        "python_version": runtime["python_version"],
        "cinderx_commit": cinderx_commit,
        "soabi": runtime["soabi"],
        "daft_version": runtime["daft_version"],
        "ray_version": runtime["ray_version"],
        "pyarrow_version": runtime["pyarrow_version"],
        "udf_jit_wheel_sha256": udf_jit_wheel_sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head-container", required=True)
    parser.add_argument("--worker-1-container", required=True)
    parser.add_argument("--worker-2-container", required=True)
    parser.add_argument("--source-git-commit", required=True)
    parser.add_argument("--cinderx-commit", required=True)
    parser.add_argument("--cinderx-source-tree-sha256", required=True)
    parser.add_argument("--cinderx-patch-sha256", required=True)
    parser.add_argument("--udf-jit-wheel-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = capture(
        containers={
            "ray-head-driver": arguments.head_container,
            "ray-worker-1": arguments.worker_1_container,
            "ray-worker-2": arguments.worker_2_container,
        },
        source_git_commit=arguments.source_git_commit,
        cinderx_commit=arguments.cinderx_commit,
        cinderx_source_tree_sha256=arguments.cinderx_source_tree_sha256,
        cinderx_patch_sha256=arguments.cinderx_patch_sha256,
        udf_jit_wheel_sha256=arguments.udf_jit_wheel_sha256,
    )
    arguments.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(arguments.output.parent, 0o700)
    descriptor = os.open(
        arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        json.dump(
            document,
            stream,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


if __name__ == "__main__":
    main()
