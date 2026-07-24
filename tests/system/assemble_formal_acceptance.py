from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from python_udf_jit.diagnostics.acceptance import (
    aggregate_formal_acceptance,
    load_acceptance_contract,
)
from tests.system.private_output import write_private_json


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return document


def assemble(
    *,
    contract_path: Path,
    base_report_path: Path,
    black_box_off_path: Path,
    black_box_auto_path: Path,
    source_path: Path,
    infrastructure_path: Path,
    measurement_path: Path,
) -> dict[str, object]:
    base_report = _load(base_report_path)
    off = _load(black_box_off_path)
    auto = _load(black_box_auto_path)
    identities = {
        (document.get("run_id"), document.get("cluster_epoch"))
        for document in (base_report, off, auto)
    }
    if len(identities) != 1:
        raise ValueError("formal acceptance inputs do not share one Run/Epoch")
    run_id, cluster_epoch = next(iter(identities))
    evidence = {
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "source": _load(source_path),
        "base_report": base_report,
        "black_box": {"off": off, "auto": auto},
        "infrastructure": _load(infrastructure_path),
        "measurement": _load(measurement_path),
    }
    return aggregate_formal_acceptance(
        load_acceptance_contract(contract_path), evidence
    )


def write_output(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--base-report", type=Path, required=True)
    parser.add_argument("--black-box-off", type=Path, required=True)
    parser.add_argument("--black-box-auto", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--infrastructure", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = assemble(
        contract_path=arguments.contract,
        base_report_path=arguments.base_report,
        black_box_off_path=arguments.black_box_off,
        black_box_auto_path=arguments.black_box_auto,
        source_path=arguments.source,
        infrastructure_path=arguments.infrastructure,
        measurement_path=arguments.measurement,
    )
    write_output(arguments.output, report)
    print(report["verdict"])


if __name__ == "__main__":
    main()
