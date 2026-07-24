from __future__ import annotations

import argparse
from pathlib import Path

from python_udf_jit.diagnostics.test_evidence import (
    build_unittest_evidence,
)
from tests.system.private_output import write_private_json


def write_output(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-id", required=True)
    parser.add_argument(
        "--tier",
        choices=("unit", "integration", "system"),
        required=True,
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-epoch", required=True)
    parser.add_argument("--source-git-commit", required=True)
    parser.add_argument("--argv", action="append", required=True)
    parser.add_argument("--required-test", action="append", required=True)
    parser.add_argument("--minimum-test-count", type=int, required=True)
    parser.add_argument("--allow-skips", action="store_true")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    proof = build_unittest_evidence(
        gate_id=arguments.gate_id,
        tier=arguments.tier,
        run_id=arguments.run_id,
        cluster_epoch=arguments.cluster_epoch,
        source_git_commit=arguments.source_git_commit,
        argv=arguments.argv,
        required_tests=arguments.required_test,
        minimum_test_count=arguments.minimum_test_count,
        allow_skips=arguments.allow_skips,
        log_path=arguments.log,
    )
    write_output(arguments.output, proof)
    print(proof["proof_sha256"])


if __name__ == "__main__":
    main()
