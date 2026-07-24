from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
)
from tests.system.capture_host_state import write_output


def build_hygiene_proof(
    *,
    run_id: str,
    cluster_epoch: str,
    retained_reports: list[Path],
    raw_event_files: list[Path],
) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    for path in retained_reports:
        if not path.is_file():
            raise FileNotFoundError(f"retained report is missing: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        reports.append(
            {
                "name": path.name,
                "mode": f"{mode:04o}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    reports.sort(key=lambda item: str(item["name"]))
    remaining = sorted(path.name for path in raw_event_files if path.exists())
    return seal_environment_proof(
        {
            "schema_version": 1,
            "status": "pass",
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "evidence_hygiene": {
                "retained_reports": reports,
                "raw_event_files_remaining": remaining,
                "raw_event_files_removed": sorted(
                    path.name for path in raw_event_files
                ),
            },
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--retained-report", type=Path, action="append", required=True)
    parser.add_argument("--raw-event-file", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    proof = build_hygiene_proof(
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        retained_reports=arguments.retained_report,
        raw_event_files=arguments.raw_event_file,
    )
    write_output(arguments.output, proof)
    print(proof["proof_sha256"])


if __name__ == "__main__":
    main()
