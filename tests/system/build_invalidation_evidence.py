from __future__ import annotations

import argparse
from pathlib import Path

from python_udf_jit.diagnostics.invalidation_evidence import (
    build_invalidation_evidence,
)
from tests.system.capture_host_state import write_output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-git-commit", required=True)
    parser.add_argument("--before-snapshot", type=Path, required=True)
    parser.add_argument("--after-snapshot", type=Path, required=True)
    parser.add_argument("--invalidated-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    proof = build_invalidation_evidence(
        source_git_commit=arguments.source_git_commit,
        before_snapshot_path=arguments.before_snapshot,
        after_snapshot_path=arguments.after_snapshot,
        invalidated_report_path=arguments.invalidated_report,
    )
    write_output(arguments.output, proof)
    print(proof["proof_sha256"])


if __name__ == "__main__":
    main()
