from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tests.system.private_output import write_private_json


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build(
    *,
    snapshots: list[dict[str, object]],
    readiness: dict[str, object],
    qualification: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, object]:
    identities = {
        (document.get("run_id"), document.get("cluster_epoch"))
        for document in [*snapshots, readiness, qualification]
    }
    if len(identities) != 1:
        raise ValueError("Run/Epoch mismatch in fresh phase evidence")
    if {snapshot.get("phase") for snapshot in snapshots} != {
        "readiness",
        "qualification",
        "e2e",
    }:
        raise ValueError("exactly one readiness/qualification/e2e snapshot is required")
    if any(
        snapshot.get("manifest_sha256") != manifest.get("candidate_manifest_sha256")
        for snapshot in snapshots
    ):
        raise ValueError("candidate manifest drift across phase snapshots")
    if any(
        snapshot.get("image_digest") != manifest.get("image_digest")
        for snapshot in snapshots
    ):
        raise ValueError("candidate image drift across phase snapshots")
    if readiness.get("manifest_sha256") != manifest.get("candidate_manifest_sha256"):
        raise ValueError("readiness manifest drift")
    required_hashes = (
        "candidate_manifest_sha256",
        "cinderx_wheel_sha256",
        "udf_jit_wheel_sha256",
    )
    if any(_SHA256.fullmatch(str(manifest.get(field, ""))) is None for field in required_hashes):
        raise ValueError("manifest hashes are invalid")
    base_image = str(manifest.get("cinderx_base_image_digest", ""))
    if (
        not base_image.startswith("sha256:")
        or _SHA256.fullmatch(base_image.removeprefix("sha256:")) is None
    ):
        raise ValueError("CinderX base image digest is invalid")
    run_id, cluster_epoch = next(iter(identities))
    return {
        "schema_version": 1,
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "manifest": manifest,
        "phase_snapshots": snapshots,
        "readiness": readiness.get("readiness", []),
        "qualification": qualification.get("qualification", []),
    }


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="ascii"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--readiness-snapshot", type=Path, required=True)
    parser.add_argument("--qualification-snapshot", type=Path, required=True)
    parser.add_argument("--e2e-snapshot", type=Path, required=True)
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--qualification-report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    inputs = (
        arguments.readiness_snapshot,
        arguments.qualification_snapshot,
        arguments.e2e_snapshot,
        arguments.readiness_report,
        arguments.qualification_report,
        arguments.manifest,
    )
    try:
        document = build(
            snapshots=[
                _load(arguments.readiness_snapshot),
                _load(arguments.qualification_snapshot),
                _load(arguments.e2e_snapshot),
            ],
            readiness=_load(arguments.readiness_report),
            qualification=_load(arguments.qualification_report),
            manifest=_load(arguments.manifest),
        )
        write_private_json(arguments.output, document)
    finally:
        for path in inputs:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
