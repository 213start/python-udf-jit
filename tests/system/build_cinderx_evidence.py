from __future__ import annotations

import argparse
from pathlib import Path

from python_udf_jit.diagnostics.cinderx_evidence import (
    build_cinderx_evidence,
)
from tests.system.private_output import write_private_json


def write_output(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cinderx-commit", required=True)
    parser.add_argument("--source-tree-sha256", required=True)
    parser.add_argument("--patch-sha256", required=True)
    parser.add_argument("--cinderx-wheel-sha256", required=True)
    parser.add_argument("--fingerprint", type=Path, required=True)
    parser.add_argument("--runtime-log", type=Path, required=True)
    parser.add_argument("--release-log", type=Path, required=True)
    parser.add_argument("--adaptive-summary", type=Path, required=True)
    parser.add_argument("--adaptive-log", type=Path, required=True)
    parser.add_argument("--official-summary", type=Path, required=True)
    parser.add_argument("--official-log", type=Path, required=True)
    parser.add_argument("--targeted-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    proof = build_cinderx_evidence(
        cinderx_commit=arguments.cinderx_commit,
        source_tree_sha256=arguments.source_tree_sha256,
        patch_sha256=arguments.patch_sha256,
        cinderx_wheel_sha256=arguments.cinderx_wheel_sha256,
        fingerprint_path=arguments.fingerprint,
        runtime_log_path=arguments.runtime_log,
        release_log_path=arguments.release_log,
        adaptive_summary_path=arguments.adaptive_summary,
        adaptive_log_path=arguments.adaptive_log,
        official_summary_path=arguments.official_summary,
        official_log_path=arguments.official_log,
        targeted_log_path=arguments.targeted_log,
    )
    write_output(arguments.output, proof)
    print(proof["proof_sha256"])


if __name__ == "__main__":
    main()
