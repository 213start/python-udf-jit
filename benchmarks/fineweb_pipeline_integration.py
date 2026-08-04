#!/usr/bin/env python3
"""Materialize a frozen Volc FineWeb task and emit value-only parity hashes.

The target framework remains the owner of task parsing, operator resolution,
Daft UDF construction, and execution grouping.  This harness only replaces the
benchmark runner's final ``count_rows()`` with a full value materialization so
an off/auto integration run can prove semantic parity without Lance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _canonical_value(value: str) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _value_hashes(values: list[str]) -> dict[str, object]:
    encoded = [_canonical_value(value) for value in values]
    row_hashes = [hashlib.sha256(value).hexdigest() for value in encoded]
    ordered = hashlib.sha256()
    for value in encoded:
        ordered.update(len(value).to_bytes(8, "big"))
        ordered.update(value)
    return {
        "multiset_sha256": hashlib.sha256(
            "".join(sorted(row_hashes)).encode("ascii")
        ).hexdigest(),
        "ordered_sha256": ordered.hexdigest(),
        "output_rows": len(values),
    }


def _materialize(task: dict[str, Any]) -> tuple[list[str], str, int]:
    import daft
    from daft import col

    from runner.input_loader import resolve_task_input
    from runner.pipeline_builder import (
        _apply_execution_group,
        _fuse_mappers_enabled,
        _native_expressions_enabled,
        _native_length_filter_enabled,
        _official_media_ops_enabled,
        _official_text_ops_enabled,
        _pipeline_execution_groups,
        _split_trailing_sink,
        init_daft_runner,
    )

    profile = dict(task.get("engine_overrides") or {})
    resolved = resolve_task_input(task, profile)
    if resolved["kind"] != "synthetic_text":
        raise ValueError("integration_harness_requires_synthetic_text")
    sink, pipeline = _split_trailing_sink(task["pipeline"])
    if sink is not None:
        raise ValueError("integration_harness_requires_no_sink")

    init_daft_runner(profile)
    field = resolved["field"]
    dataframe = daft.from_pydict({field: resolved["data"]})
    requested_partitions = profile.get("into_partitions")
    if requested_partitions:
        dataframe = dataframe.into_partitions(int(requested_partitions))

    fuse_mappers = _fuse_mappers_enabled(profile)
    native_expressions = _native_expressions_enabled(profile)
    official_text_ops = _official_text_ops_enabled(profile)
    official_media_ops = _official_media_ops_enabled(profile)
    if native_expressions or official_text_ops or official_media_ops:
        fuse_mappers = False
    groups = _pipeline_execution_groups(
        pipeline,
        fuse_mappers=fuse_mappers,
    )
    current_field = field
    for group_index, (group_kind, steps) in enumerate(groups):
        dataframe, current_field = _apply_execution_group(
            dataframe,
            current_field,
            group_kind,
            steps,
            native_length_filter=_native_length_filter_enabled(profile),
            native_expressions=native_expressions,
            official_text_ops=official_text_ops,
            official_media_ops=official_media_ops,
            group_idx=group_index,
        )
    values = dataframe.select(
        col(current_field).alias("value")
    ).to_pydict()["value"]
    if not all(type(value) is str for value in values):
        raise TypeError("integration_output_must_be_exact_str")
    return values, current_field, len(groups)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    task_bytes = arguments.task.read_bytes()
    task = json.loads(task_bytes)
    values, output_column, execution_stages = _materialize(task)
    document = {
        **_value_hashes(values),
        "diagnostics": os.environ.get("UDFJIT_DIAGNOSTICS", "off"),
        "execution_stages": execution_stages,
        "mode": os.environ.get("UDFJIT_MODE", "off"),
        "output_column": output_column,
        "pipeline_ops": [step["dj_ops"] for step in task["pipeline"]],
        "schema_version": 1,
        "task_sha256": hashlib.sha256(task_bytes).hexdigest(),
    }
    arguments.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
