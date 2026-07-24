from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import socket
from pathlib import Path
from typing import Any, Callable


EXCEPTION_SENTINEL = "U13-TRANSPARENT-USER-ERROR"
USER_EXCEPTION_NAME = "TransparentUserError"


def supported_measurement(value: float) -> float:
    return value * 2.0 + 3.0


def adjusted_measurement(value: float) -> float:
    return value - 4.0


def _side_effect_path(scenario: str) -> Path:
    run_id = os.environ["UDFJIT_RUN_ID"]
    return Path(f"/tmp/udfjit-black-box-{run_id}-{scenario}-{os.getpid()}.count")


def _append_side_effect(scenario: str) -> None:
    descriptor = os.open(
        _side_effect_path(scenario),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(b"call\n")


def unsupported_measurement(value: float) -> float:
    _append_side_effect("unsupported")
    return round(value * 1.5 + 1.25, 8)


class TransparentUserError(RuntimeError):
    pass


def raising_measurement(_value: float) -> float:
    _append_side_effect("exception")
    raise TransparentUserError(EXCEPTION_SENTINEL)


def _normalized_value(value: Any) -> object:
    if isinstance(value, float):
        return {"float_hex": value.hex()}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported black-box result type: {type(value).__name__}")


def ordered_result_sha256(rows: list[dict[str, Any]]) -> str:
    payload = [
        [[key, _normalized_value(value)] for key, value in row.items()]
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _schema_sha256(schema: object) -> str:
    return hashlib.sha256(repr(schema).encode("utf-8")).hexdigest()


def exception_observation(error: BaseException) -> dict[str, object]:
    texts = []
    cursor: BaseException | None = error
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        texts.extend(
            (
                type(cursor).__name__,
                type(cursor).__qualname__,
                str(cursor),
                repr(cursor),
            )
        )
        cursor = cursor.__cause__ or cursor.__context__
    joined = "\n".join(texts)
    return {
        "exception_type": f"{type(error).__module__}.{type(error).__qualname__}",
        "user_exception_type_observed": USER_EXCEPTION_NAME in joined,
        "message_sentinel_observed": EXCEPTION_SENTINEL in joined,
    }


def _document_rows(
    document: dict[str, list[Any]], columns: tuple[str, ...]
) -> list[dict[str, Any]]:
    lengths = {len(document[column]) for column in columns}
    if len(lengths) != 1:
        raise AssertionError("black-box result columns have different lengths")
    row_count = next(iter(lengths), 0)
    return [
        {column: document[column][index] for column in columns}
        for index in range(row_count)
    ]


def _scenario(
    *,
    document: dict[str, list[Any]],
    columns: tuple[str, ...],
    schema: object,
    callable_calls: int = 0,
) -> dict[str, object]:
    rows = _document_rows(document, columns)
    return {
        "completed": True,
        "ordered_result_sha256": ordered_result_sha256(rows),
        "schema_sha256": _schema_sha256(schema),
        "row_count": len(rows),
        "callable_calls": callable_calls,
        "side_effect_count": callable_calls,
    }


def _write_fixture(directory: str) -> dict[str, object]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    partitions = (
        ([0, 2], [0.0, -2.5]),
        ([1, 3], [1.25, 9.0]),
    )
    for index, (row_ids, values) in enumerate(partitions):
        pq.write_table(
            pa.table(
                {
                    "row_id": pa.array(row_ids, type=pa.int64()),
                    "measurement": pa.array(values, type=pa.float64()),
                }
            ),
            root / f"part-{index}.parquet",
        )
    pq.write_table(
        pa.table(
            {
                "row_id": pa.array([], type=pa.int64()),
                "measurement": pa.array([], type=pa.float64()),
            }
        ),
        root / "empty.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "row_id": pa.array([0], type=pa.int64()),
                "measurement": pa.array([1.25], type=pa.float64()),
            }
        ),
        root / "exception.parquet",
    )
    return {"hostname": socket.gethostname(), "file_count": 4}


def _remove_fixture(directory: str) -> str:
    shutil.rmtree(directory, ignore_errors=True)
    return socket.gethostname()


def _collect_side_effects(run_id: str, scenario: str) -> dict[str, object]:
    count = 0
    for filename in glob.glob(
        f"/tmp/udfjit-black-box-{run_id}-{scenario}-*.count"
    ):
        path = Path(filename)
        try:
            count += len(path.read_bytes().splitlines())
        finally:
            path.unlink(missing_ok=True)
    return {"hostname": socket.gethostname(), "count": count}


def _node_tasks(
    function: Callable[..., Any], worker_nodes: list[dict[str, Any]], *args: Any
) -> list[Any]:
    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    remote = ray.remote(num_cpus=1)(function)
    references = [
        remote.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                node_id=node["NodeID"],
                soft=False,
            )
        ).remote(*args)
        for node in worker_nodes
    ]
    return ray.get(references)


def _side_effect_count(
    worker_nodes: list[dict[str, Any]], scenario: str
) -> int:
    reports = _node_tasks(
        _collect_side_effects,
        worker_nodes,
        os.environ["UDFJIT_RUN_ID"],
        scenario,
    )
    return sum(int(report["count"]) for report in reports)


def _input_frame(directory: str, pattern: str = "part-*.parquet"):
    import daft

    return daft.read_parquet(f"{directory}/{pattern}")


def _with_column(directory: str) -> dict[str, object]:
    import daft

    projection = daft.func(supported_measurement)
    result = (
        _input_frame(directory)
        .with_column("result", projection(daft.col("measurement")))
        .sort("row_id")
        .select("row_id", "measurement", "result")
    )
    schema = result.schema()
    return _scenario(
        document=result.to_pydict(),
        columns=("row_id", "measurement", "result"),
        schema=schema,
    )


def _with_columns(directory: str) -> dict[str, object]:
    import daft

    supported = daft.func(supported_measurement)
    adjusted = daft.func(adjusted_measurement)
    result = (
        _input_frame(directory)
        .with_columns(
            {
                "result": supported(daft.col("measurement")),
                "adjusted": adjusted(daft.col("measurement")),
            }
        )
        .sort("row_id")
        .select("row_id", "measurement", "result", "adjusted")
    )
    schema = result.schema()
    return _scenario(
        document=result.to_pydict(),
        columns=("row_id", "measurement", "result", "adjusted"),
        schema=schema,
    )


def _unsupported(
    directory: str, worker_nodes: list[dict[str, Any]]
) -> dict[str, object]:
    import daft

    _side_effect_count(worker_nodes, "unsupported")
    projection = daft.func(unsupported_measurement)
    result = (
        _input_frame(directory)
        .with_column("result", projection(daft.col("measurement")))
        .sort("row_id")
        .select("row_id", "measurement", "result")
    )
    schema = result.schema()
    document = result.to_pydict()
    calls = _side_effect_count(worker_nodes, "unsupported")
    return _scenario(
        document=document,
        columns=("row_id", "measurement", "result"),
        schema=schema,
        callable_calls=calls,
    )


def _exception(
    directory: str, worker_nodes: list[dict[str, Any]]
) -> dict[str, object]:
    import daft

    _side_effect_count(worker_nodes, "exception")
    projection = daft.func(raising_measurement)
    try:
        (
            _input_frame(directory, "exception.parquet")
            .with_column("result", projection(daft.col("measurement")))
            .select("row_id", "measurement", "result")
            .to_pydict()
        )
    except BaseException as error:
        observation = exception_observation(error)
    else:
        raise AssertionError("exception scenario unexpectedly completed")
    calls = _side_effect_count(worker_nodes, "exception")
    return {
        "completed": True,
        **observation,
        "callable_calls": calls,
        "side_effect_count": calls,
    }


def _zero_row(directory: str) -> dict[str, object]:
    import daft

    projection = daft.func(supported_measurement)
    result = (
        _input_frame(directory, "empty.parquet")
        .with_column("result", projection(daft.col("measurement")))
        .select("row_id", "measurement", "result")
    )
    schema = result.schema()
    return _scenario(
        document=result.to_pydict(),
        columns=("row_id", "measurement", "result"),
        schema=schema,
    )


def _bootstrap_hooks_installed() -> bool:
    from daft.dataframe.dataframe import DataFrame
    from daft.udf.udf_v2 import Func

    return bool(
        callable(getattr(Func.__call__, "__wrapped__", None))
        and callable(getattr(DataFrame.with_columns, "__wrapped__", None))
    )


def run_black_box_job() -> dict[str, object]:
    import daft
    import ray

    mode = os.environ.get("UDFJIT_MODE", "")
    if mode not in {"off", "auto"}:
        raise RuntimeError("black-box Ray Job requires mode off or auto")
    run_id = os.environ["UDFJIT_RUN_ID"]
    cluster_epoch = os.environ["UDFJIT_CLUSTER_EPOCH"]
    fixture_directory = f"/tmp/udfjit-black-box-{run_id}-{mode}"

    ray.init(address="auto")
    daft.set_runner_ray(address="auto", noop_if_initialized=True)
    alive = [node for node in ray.nodes() if node.get("Alive")]
    heads = [node for node in alive if node.get("NodeName") == "ray-head-driver"]
    workers = sorted(
        [
            node
            for node in alive
            if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
        ],
        key=lambda node: node["NodeName"],
    )
    if len(heads) != 1 or len(workers) != 2:
        raise RuntimeError("black-box job requires the fixed three-node topology")
    if heads[0].get("Resources", {}).get("CPU", 0) != 0:
        raise RuntimeError("black-box Head must have zero logical CPU")
    if ray.get_runtime_context().get_node_id() != heads[0]["NodeID"]:
        raise RuntimeError("black-box Driver did not run on Head")

    _write_fixture(fixture_directory)
    _node_tasks(_write_fixture, workers, fixture_directory)
    try:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "mode": mode,
            "user_script_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            "plugin_import_count": 0,
            "bootstrap_hooks_installed": _bootstrap_hooks_installed(),
            "driver_node_id": ray.get_runtime_context().get_node_id(),
            "worker_node_ids": [node["NodeID"] for node in workers],
            "scenarios": {
                "with_column": _with_column(fixture_directory),
                "with_columns": _with_columns(fixture_directory),
                "unsupported": _unsupported(fixture_directory, workers),
                "exception": _exception(fixture_directory, workers),
                "zero_row": _zero_row(fixture_directory),
            },
        }
    finally:
        _remove_fixture(fixture_directory)
        _node_tasks(_remove_fixture, workers, fixture_directory)
        ray.shutdown()


def write_output(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    write_output(arguments.output, run_black_box_job())


if __name__ == "__main__":
    main()
