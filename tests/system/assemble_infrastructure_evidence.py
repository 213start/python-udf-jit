from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Any, Mapping

from tests.system.capture_host_state import write_output


def _load(path: Path) -> dict[str, Any]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"{path} must be mode 0600")
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return document


def assemble(
    *,
    run_id: str,
    cluster_epoch: str,
    cinderx: Mapping[str, Any],
    unit: Mapping[str, Any],
    integration: Mapping[str, Any],
    live: Mapping[str, Any],
    auth: Mapping[str, Any],
    invalidation: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    hygiene: Mapping[str, Any],
) -> dict[str, object]:
    identity_bound = {
        "unit": unit,
        "integration": integration,
        "live": live,
        "auth": auth,
        "invalidation": invalidation,
        "cleanup": cleanup,
        "hygiene": hygiene,
    }
    for name, proof in identity_bound.items():
        if (
            proof.get("run_id") != run_id
            or proof.get("cluster_epoch") != cluster_epoch
        ):
            raise ValueError(f"{name} proof Run/Epoch mismatch")
    if cinderx.get("schema_version") != 1 or cinderx.get("status") != "pass":
        raise ValueError("CinderX proof is not a completed schema-v1 proof")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "cinderx": dict(cinderx),
        "python_test_proofs": {
            "unit": dict(unit),
            "integration": dict(integration),
            "live": dict(live),
        },
        "environment_auth": dict(auth),
        "invalidation": dict(invalidation),
        "environment_cleanup": dict(cleanup),
        "evidence_hygiene": dict(hygiene),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--cinderx", type=Path, required=True)
    parser.add_argument("--unit", type=Path, required=True)
    parser.add_argument("--integration", type=Path, required=True)
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--invalidation", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--hygiene", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = assemble(
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        cinderx=_load(arguments.cinderx),
        unit=_load(arguments.unit),
        integration=_load(arguments.integration),
        live=_load(arguments.live),
        auth=_load(arguments.auth),
        invalidation=_load(arguments.invalidation),
        cleanup=_load(arguments.cleanup),
        hygiene=_load(arguments.hygiene),
    )
    write_output(arguments.output, document)
    print("pass")


if __name__ == "__main__":
    main()
