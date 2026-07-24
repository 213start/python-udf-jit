from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


_ROLES = ("ray-head-driver", "ray-worker-1", "ray-worker-2")


def _docker_json(*arguments: str) -> object:
    completed = subprocess.run(
        ["docker", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def _container_observation(container: str, role: str) -> dict[str, str]:
    documents = _docker_json("inspect", container)
    if not isinstance(documents, list) or len(documents) != 1:
        raise RuntimeError(f"docker inspect did not resolve exactly one {role} container")
    document = documents[0]
    if document["Config"]["Hostname"] != role or document["State"]["Running"] is not True:
        raise RuntimeError(f"{role} container identity/state drift")
    boot_material = f"{document['Id']}\0{document['State']['StartedAt']}".encode("utf-8")
    return {
        "role": role,
        "container_id": document["Id"],
        "container_boot_id": hashlib.sha256(boot_material).hexdigest(),
        "image_digest": document["Image"],
    }


def _ray_nodes(head_container: str) -> dict[str, str]:
    source = (
        "import json,ray;ray.init(address='auto');"
        "print(json.dumps({n['NodeName']:n['NodeID'] for n in ray.nodes() if n.get('Alive')},sort_keys=True))"
    )
    completed = subprocess.run(
        ["docker", "exec", head_container, "python", "-c", source],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError("Ray node snapshot missing from Head")
    nodes = json.loads(lines[-1])
    if set(nodes) != set(_ROLES):
        raise RuntimeError("Ray node/role mapping drift")
    return {str(role): str(node_id) for role, node_id in nodes.items()}


def capture(
    *,
    phase: str,
    run_id: str,
    cluster_epoch: str,
    manifest_sha256: str,
    containers: dict[str, str],
) -> dict[str, object]:
    observations = [
        _container_observation(containers[role], role) for role in _ROLES
    ]
    if len({item["image_digest"] for item in observations}) != 1:
        raise RuntimeError("candidate image drift across the three containers")
    node_ids = _ray_nodes(containers["ray-head-driver"])
    return {
        "phase": phase,
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "manifest_sha256": manifest_sha256,
        "image_digest": observations[0]["image_digest"],
        "nodes": [
            {
                "role": item["role"],
                "node_id": node_ids[item["role"]],
                "container_boot_id": item["container_boot_id"],
            }
            for item in observations
        ],
    }


def _write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        json.dump(
            document,
            stream,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("readiness", "qualification", "e2e"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--head-container", required=True)
    parser.add_argument("--worker-1-container", required=True)
    parser.add_argument("--worker-2-container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = capture(
        phase=arguments.phase,
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        manifest_sha256=arguments.manifest_sha256,
        containers={
            "ray-head-driver": arguments.head_container,
            "ray-worker-1": arguments.worker_1_container,
            "ray-worker-2": arguments.worker_2_container,
        },
    )
    _write(arguments.output, document)


if __name__ == "__main__":
    main()
