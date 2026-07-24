from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from pathlib import Path
from typing import Any, Callable


def _calibrate(value: float) -> float:
    return value * 1.5 + 1.25


def _result_digest(document: dict[str, list[Any]]) -> str:
    rows = sorted(
        (float(value).hex(), float(result).hex())
        for value, result in zip(document["measurement"], document["result"], strict=True)
    )
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _time(call: Callable[[], str]) -> tuple[int, str]:
    started = time.perf_counter_ns()
    digest = call()
    return time.perf_counter_ns() - started, digest


def _build_runner(mode: str) -> Callable[[], str]:
    import daft
    from daft.dataframe.dataframe import DataFrame
    from daft.udf.udf_v2 import Func

    from python_udf_jit.integration.daft_ray.control import (
        install_default_daft_hooks,
        uninstall_daft_control_hooks,
    )

    values = [-7.0, -0.0, 0.25, 1.5, 9.0, 32.0]
    if mode == "off":
        # Build an expression through the locked Daft originals. Reinstalling after
        # construction does not rewrite that expression, so the Worker receives the
        # original callable and this sample contains no UDF-JIT Wrapper.
        uninstall_daft_control_hooks(Func, DataFrame)
        try:
            projection = daft.func(_calibrate)
            dataframe = (
                daft.from_pydict({"measurement": values})
                .repartition(2)
                .with_column("result", projection(daft.col("measurement")))
                .select("measurement", "result")
            )
        finally:
            install_default_daft_hooks(daft)
    elif mode == "auto":
        install_default_daft_hooks(daft)
        projection = daft.func(_calibrate)
        dataframe = (
            daft.from_pydict({"measurement": values})
            .repartition(2)
            .with_column("result", projection(daft.col("measurement")))
            .select("measurement", "result")
        )
    else:
        raise ValueError("mode must be off or auto")

    def execute() -> str:
        return _result_digest(dataframe.to_pydict())

    return execute


def _environment() -> dict[str, str]:
    import daft
    import pyarrow
    import ray

    manifest_path = Path(os.environ["UDFJIT_MANIFEST_PATH"])
    return {
        "python_version": platform.python_version(),
        "platform_machine": platform.machine(),
        "daft_version": daft.__version__,
        "ray_version": ray.__version__,
        "pyarrow_version": pyarrow.__version__,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def measure(samples: int) -> dict[str, object]:
    if samples < 1 or samples > 20:
        raise ValueError("samples must be in [1, 20]")
    if os.environ.get("UDFJIT_LIVE_RAY") != "1":
        raise RuntimeError("measurement requires UDFJIT_LIVE_RAY=1")

    import daft

    daft.set_runner_ray(address="auto", noop_if_initialized=True)
    off = _build_runner("off")
    auto = _build_runner("auto")

    off_warmup_ns, off_digest = _time(off)
    off_samples = [_time(off)[0] for _ in range(samples)]
    auto_cold_compile_window_ns, auto_digest = _time(auto)
    auto_samples = [_time(auto)[0] for _ in range(samples)]
    if off_digest != auto_digest:
        raise AssertionError("off/auto result digest mismatch")
    return {
        "schema_version": 1,
        "run_id": os.environ["UDFJIT_RUN_ID"],
        "cluster_epoch": os.environ["UDFJIT_CLUSTER_EPOCH"],
        "measurement_scope": "small_e2e_validation_not_release_performance",
        "units": "nanoseconds",
        "sample_count": samples,
        "warmup_count": 1,
        "environment": _environment(),
        "off": {
            "warmup_ns": off_warmup_ns,
            "samples_ns": off_samples,
            "median_ns": int(statistics.median(off_samples)),
            "result_digest": off_digest,
        },
        "auto": {
            "cold_compile_window_ns": auto_cold_compile_window_ns,
            "samples_ns": auto_samples,
            "median_ns": int(statistics.median(auto_samples)),
            "result_digest": auto_digest,
        },
        "result_equivalent": True,
        "speedup_gate_applied": False,
        "notes": [
            "cold_compile_window_ns includes scheduling, serialization, compilation, and execution",
            "samples are validation-scale observations and are not a 1.15x release claim",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/scalar-piercing/measurement.json"),
    )
    arguments = parser.parse_args()
    report = measure(arguments.samples)
    _write_report(arguments.output, report)
    print(arguments.output)


if __name__ == "__main__":
    main()
