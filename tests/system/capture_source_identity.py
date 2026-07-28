from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from python_udf_jit.diagnostics.cinderx_evidence import (
    validate_cinderx_evidence,
)
from tests.system.private_output import write_private_json


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _run(arguments: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout


def _private_document(path: Path) -> Mapping[str, Any]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"{path} must be mode 0600")
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, Mapping):
        raise ValueError(f"{path} must contain one JSON object")
    return document


def source_document(
    *,
    git_commit: str,
    dirty: bool,
    image_digest: str,
    image_labels: Mapping[str, str],
    udf_jit_wheel_sha256: str,
    cinderx_wheel_sha256: str,
    cinderx_base_image_digest: str,
    cinderx_proof: Mapping[str, Any],
    patch_sha256: str,
) -> dict[str, object]:
    identity = cinderx_proof.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("CinderX proof identity is missing")
    cinderx_commit = identity.get("cinderx_commit")
    source_tree = identity.get("source_tree_sha256")
    expected_labels = {
        "org.opencontainers.image.revision": git_commit,
        "org.python-udf-jit.cinderx-commit": cinderx_commit,
        "org.python-udf-jit.cinderx-source-tree-sha256": source_tree,
        "org.python-udf-jit.cinderx-patch-sha256": patch_sha256,
        "org.python-udf-jit.cinderx-wheel-sha256": cinderx_wheel_sha256,
        "org.python-udf-jit.cinderx-base-image-digest":
            cinderx_base_image_digest,
        "org.python-udf-jit.wheel-sha256": udf_jit_wheel_sha256,
    }
    if (
        _GIT_COMMIT.fullmatch(git_commit) is None
        or dirty
        or not isinstance(image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
        or not isinstance(cinderx_commit, str)
        or _GIT_COMMIT.fullmatch(cinderx_commit) is None
        or not isinstance(source_tree, str)
        or _SHA256.fullmatch(source_tree) is None
        or _SHA256.fullmatch(udf_jit_wheel_sha256) is None
        or _SHA256.fullmatch(cinderx_wheel_sha256) is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            cinderx_base_image_digest,
        )
        is None
        or _SHA256.fullmatch(patch_sha256) is None
        or identity.get("patch_sha256") != patch_sha256
        or identity.get("cinderx_wheel_sha256") != cinderx_wheel_sha256
        or identity.get("image_digest") != cinderx_base_image_digest
        or validate_cinderx_evidence(cinderx_proof) != "pass"
        or any(image_labels.get(name) != value for name, value in expected_labels.items())
    ):
        raise ValueError("source, CinderX proof, wheel, and image labels do not match")
    return {
        "git_commit": git_commit,
        "dirty": False,
        "cinderx_commit": cinderx_commit,
        "cinderx_source_tree_sha256": source_tree,
        "cinderx_patch_sha256": patch_sha256,
        "cinderx_wheel_sha256": cinderx_wheel_sha256,
        "cinderx_base_image_digest": cinderx_base_image_digest,
        "image_digest": image_digest,
        "udf_jit_wheel_sha256": udf_jit_wheel_sha256,
    }


def capture(
    *,
    repository: Path,
    image: str,
    udf_jit_wheel: Path,
    cinderx_wheel: Path,
    cinderx_base_image_digest: str,
    cinderx_proof_path: Path,
    patch_paths: Sequence[Path],
) -> dict[str, object]:
    repository = repository.resolve()
    patches = tuple(path.resolve() for path in patch_paths)
    if not patches:
        raise ValueError("CinderX patch series must not be empty")
    patch_relatives = []
    for patch in patches:
        try:
            patch_relatives.append(patch.relative_to(repository))
        except ValueError as error:
            raise ValueError(
                "CinderX patches must be inside the Git repository"
            ) from error
    git_commit = _run(["git", "rev-parse", "HEAD"], cwd=repository).strip()
    status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
    )
    for patch_relative in patch_relatives:
        _run(
            [
                "git",
                "ls-files",
                "--error-unmatch",
                patch_relative.as_posix(),
            ],
            cwd=repository,
        )
    if not udf_jit_wheel.is_file():
        raise FileNotFoundError(f"UDF JIT wheel is missing: {udf_jit_wheel}")
    if not cinderx_wheel.is_file():
        raise FileNotFoundError(f"CinderX wheel is missing: {cinderx_wheel}")
    wheel_digest = hashlib.sha256(udf_jit_wheel.read_bytes()).hexdigest()
    cinderx_wheel_digest = hashlib.sha256(
        cinderx_wheel.read_bytes()
    ).hexdigest()
    patch_hasher = hashlib.sha256()
    for patch in patches:
        patch_hasher.update(patch.read_bytes())
    patch_digest = patch_hasher.hexdigest()
    cinderx_proof = _private_document(cinderx_proof_path)
    image_documents = json.loads(
        _run(["docker", "image", "inspect", image])
    )
    if not isinstance(image_documents, list) or len(image_documents) != 1:
        raise RuntimeError("Docker image reference did not resolve exactly once")
    image_document = image_documents[0]
    labels = image_document.get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict):
        raise RuntimeError("candidate image labels are missing")
    return source_document(
        git_commit=git_commit,
        dirty=bool(status),
        image_digest=str(image_document.get("Id", "")),
        image_labels={str(key): str(value) for key, value in labels.items()},
        udf_jit_wheel_sha256=wheel_digest,
        cinderx_wheel_sha256=cinderx_wheel_digest,
        cinderx_base_image_digest=cinderx_base_image_digest,
        cinderx_proof=cinderx_proof,
        patch_sha256=patch_digest,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--udf-jit-wheel", type=Path, required=True)
    parser.add_argument("--cinderx-wheel", type=Path, required=True)
    parser.add_argument("--cinderx-base-image-digest", required=True)
    parser.add_argument("--cinderx-proof", type=Path, required=True)
    parser.add_argument(
        "--patch",
        dest="patches",
        action="append",
        type=Path,
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = capture(
        repository=arguments.repository,
        image=arguments.image,
        udf_jit_wheel=arguments.udf_jit_wheel,
        cinderx_wheel=arguments.cinderx_wheel,
        cinderx_base_image_digest=arguments.cinderx_base_image_digest,
        cinderx_proof_path=arguments.cinderx_proof,
        patch_paths=arguments.patches,
    )
    write_private_json(arguments.output, document)
    print(document["git_commit"])


if __name__ == "__main__":
    main()
