from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from python_udf_jit.diagnostics.evidence import EvidenceRun


def _merge_scenarios(
    off_observation: dict[str, Any], auto_observation: dict[str, Any]
) -> dict[str, object]:
    off = off_observation["scenarios"]
    auto = auto_observation["scenarios"]
    row_count = int(auto["supported"]["row_count"])
    return {
        "supported": {
            "completed": True,
            "off_result_digest": off["supported"]["result_digest"],
            "auto_result_digest": auto["supported"]["result_digest"],
            "row_count": row_count,
            "callable_calls": auto["supported"]["callable_calls"],
            "side_effect_count": auto["supported"]["side_effect_count"],
        },
        "guard_miss": {
            "completed": True,
            "off_result_digest": off["guard_miss"]["result_digest"],
            "auto_result_digest": auto["guard_miss"]["result_digest"],
            "row_count": row_count,
            "off_callable_calls": off["guard_miss"]["callable_calls"],
            "auto_callable_calls": auto["guard_miss"]["callable_calls"],
            "fallback_count": auto["guard_miss"]["fallback_count"],
            "side_effect_count": auto["guard_miss"]["side_effect_count"],
            "semantic_execute_count": auto["guard_miss"]["semantic_execute_count"],
            "reason_code": auto["guard_miss"]["reason_code"],
        },
        "unsupported": {
            "completed": True,
            "off_result_digest": off["unsupported"]["result_digest"],
            "auto_result_digest": auto["unsupported"]["result_digest"],
            "row_count": row_count,
            "off_callable_calls": off["unsupported"]["callable_calls"],
            "auto_callable_calls": auto["unsupported"]["callable_calls"],
            "side_effect_count": auto["unsupported"]["side_effect_count"],
            "reason_code": auto["unsupported"]["reason_code"],
        },
        "mode_off": {
            "completed": off["mode_off"]["completed"],
            "result_digest": off["mode_off"]["result_digest"],
            "expected_result_digest": off["supported"]["result_digest"],
            "reason_code": "mode_off",
        },
        "fingerprint_mismatch": {
            **auto["fingerprint_mismatch"],
            "expected_result_digest": off["supported"]["result_digest"],
        },
        "corrupt_artifact": {
            **auto["corrupt_artifact"],
            "expected_result_digest": off["corrupt_artifact"]["result_digest"],
            "off_callable_calls": off["corrupt_artifact"]["callable_calls"],
        },
        "zero_row": {
            "completed": True,
            "off_result_digest": off["zero_row"]["result_digest"],
            "auto_result_digest": auto["zero_row"]["result_digest"],
            "row_count": auto["zero_row"]["row_count"],
            "callable_calls": auto["zero_row"]["callable_calls"],
            "descriptor_count": auto["zero_row"]["descriptor_count"],
            "compile_count": auto["zero_row"]["compile_count"],
            "hit_count": auto["zero_row"]["hit_count"],
            "activity_event_count": auto["zero_row"][
                "activity_event_count"
            ],
        },
    }


def assemble(
    off_observation: dict[str, Any],
    auto_observation: dict[str, Any],
    phase_evidence: dict[str, Any],
    *,
    raw_root: Path,
    output: Path,
) -> dict[str, object]:
    """Join fresh stage evidence; the aggregator owns all pass/fail decisions."""

    if off_observation.get("job_mode") != "off" or auto_observation.get("job_mode") != "auto":
        raise ValueError("both real off and auto Ray Job observations are required")
    identities = {
        (document.get("run_id"), document.get("cluster_epoch"))
        for document in (off_observation, auto_observation, phase_evidence)
    }
    if len(identities) != 1:
        raise ValueError("Run/Epoch mismatch across Job and phase evidence")
    if off_observation.get("topology") != auto_observation.get("topology"):
        raise ValueError("topology drift between off and auto Ray Jobs")
    if off_observation.get("manifest_sha256") != auto_observation.get("manifest_sha256"):
        raise ValueError("manifest drift between off and auto Ray Jobs")
    evidence = {
        "run_id": auto_observation["run_id"],
        "cluster_epoch": auto_observation["cluster_epoch"],
        "manifest": phase_evidence.get("manifest"),
        "phase_snapshots": phase_evidence.get("phase_snapshots"),
        "topology": auto_observation.get("topology"),
        "readiness": phase_evidence.get("readiness"),
        "qualification": phase_evidence.get("qualification"),
        "scenarios": _merge_scenarios(off_observation, auto_observation),
    }
    run = EvidenceRun(raw_root, str(evidence["run_id"]))
    try:
        for event in auto_observation.get("raw_events", []):
            run.append_event(event)
        return run.finalize(evidence, output)
    except Exception:
        # A malformed event is not allowed to leave a value-bearing raw file behind.
        if run.raw_dir.exists():
            import shutil

            shutil.rmtree(run.raw_dir)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--off-observation", type=Path, required=True)
    parser.add_argument("--auto-observation", type=Path, required=True)
    parser.add_argument("--phase-evidence", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    inputs = (
        arguments.off_observation,
        arguments.auto_observation,
        arguments.phase_evidence,
    )
    try:
        off = json.loads(arguments.off_observation.read_text(encoding="ascii"))
        auto = json.loads(arguments.auto_observation.read_text(encoding="ascii"))
        phases = json.loads(arguments.phase_evidence.read_text(encoding="ascii"))
        report = assemble(
            off,
            auto,
            phases,
            raw_root=arguments.raw_root,
            output=arguments.output,
        )
        print(report["verdict"])
    finally:
        for path in inputs:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
