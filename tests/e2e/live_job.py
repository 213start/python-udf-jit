from __future__ import annotations

import argparse
import functools
import glob
import hashlib
import json
import os
import shutil
import socket
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from tests.system.private_output import write_private_json


_FIXTURE_PARTITION_COUNT = 32
_SOURCES_PER_SCAN_TASK = 8
_MIN_CPU_PER_TASK = 2.0


def _supported(value: float) -> float:
    return value * 2.0 + 3.0


def _guard_value(value: float) -> float:
    return value * 1.5 + 1.25


def _corrupt_value(value: float) -> float:
    return value * 3.0 - 2.0


def _side_effect_path(scenario: str) -> Path:
    run_id = os.environ["UDFJIT_RUN_ID"]
    return Path(f"/tmp/udfjit-{run_id}-{scenario}-{os.getpid()}.count")


def _append_side_effect(scenario: str) -> None:
    path = _side_effect_path(scenario)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(b"call\n")


def _unsupported_side_effect(value: float) -> float:
    _append_side_effect("unsupported")
    return round(value * 1.5 + 1.25, 8)


@functools.wraps(_guard_value)
def _guard_side_effect_method(_self: object, value: float) -> float:
    _append_side_effect("guard")
    return _guard_value(value)


@functools.wraps(_corrupt_value)
def _corrupt_side_effect_method(_self: object, value: float) -> float:
    _append_side_effect("corrupt")
    return _corrupt_value(value)


def _guard_wrapper_class():
    from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper
    from python_udf_jit.integration.daft_ray.worker import (
        WorkerGuardOverrides,
        build_default_worker_adapter,
    )

    class GuardMissWrapper(FallbackOnlyWrapper):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            if os.environ.get("UDFJIT_MODE", "off") != "auto":
                return self._fallback(args, kwargs, "u2_fallback_only")
            adapter = self._worker_adapter
            if adapter is not None and getattr(adapter, "owner_pid", None) != os.getpid():
                adapter = None
                self._worker_adapter = None
            if adapter is None:
                try:
                    adapter = build_default_worker_adapter(self)
                    self._worker_adapter = adapter
                except Exception as error:
                    return self._fallback(
                        args,
                        kwargs,
                        f"worker_adapter_init_failed:{type(error).__name__}",
                    )
            return adapter.invoke(
                args,
                kwargs,
                guard_overrides=WorkerGuardOverrides(
                    logical_schema="{'controlled': 'schema_mismatch'}"
                ),
            )

    return GuardMissWrapper


def _diagnostic_probe_factory(started_ns: int):
    def diagnostic_probe(_value: float) -> str:
        import json as _json
        import os as _os

        import ray as _ray

        from python_udf_jit.diagnostics.report import DEFAULT_RUNTIME_REPORT

        runtime = _ray.get_runtime_context()
        pid = _os.getpid()
        run_id = _os.environ.get("UDFJIT_RUN_ID", "")
        events = []
        for event in DEFAULT_RUNTIME_REPORT.snapshot():
            if (
                event.run_id != run_id
                or event.process.pid != pid
                or event.timestamp_ns < started_ns
            ):
                continue
            events.append(
                {
                    "stage": event.stage,
                    "decision": event.decision,
                    "reason_code": event.reason_code,
                    "run_id": event.run_id,
                    "cluster_epoch": event.process.cluster_epoch,
                    "node_id": event.process.node_id,
                    "actor_id": event.process.actor_worker_id,
                    "pid": event.process.pid,
                    "process_generation": event.process.process_generation,
                    "partition_id": event.partition_id,
                    "task_attempt": event.task_attempt,
                    "variant_key": event.variant_key,
                    "artifact_hash": event.artifact_hash,
                    "code_hash": event.code_hash,
                    "execution_mode": event.execution_mode,
                    "timestamp_ns": event.timestamp_ns,
                }
            )
        return _json.dumps(
            {
                "runtime_task_id": str(runtime.get_task_id()),
                "runtime_node_id": runtime.get_node_id(),
                "runtime_actor_id": str(runtime.get_actor_id()),
                "runtime_worker_id": str(runtime.get_worker_id()),
                "pid": pid,
                "events": events,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    return diagnostic_probe


def _result_digest(document: dict[str, list[Any]]) -> str:
    rows = sorted(
        (float(value).hex(), float(result).hex())
        for value, result in zip(
            document["measurement"], document["result"], strict=True
        )
    )
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _make_artifact(function: Callable[[float], float]) -> bytes:
    from python_udf_jit.compiler.capture import CaptureRequest, capture
    from python_udf_jit.compiler.pipeline import compile_semantic
    from python_udf_jit.protocol.artifact import build_artifact
    from python_udf_jit.protocol.codec import encode_artifact

    captured = capture(CaptureRequest(function))
    compiled = compile_semantic(captured)
    return encode_artifact(
        build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
    )


def _artifact_wrapper(
    *,
    pure_function: Callable[[float], float],
    original_method: Callable[..., float],
    logical_schema: str,
    manifest_sha256: str,
    guard_miss: bool = False,
    corrupt: bool = False,
):
    from python_udf_jit.integration.daft_ray.carrier import (
        InlineArtifactHandle,
        ProductionCarrierState,
    )
    from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper

    candidate_id = hashlib.sha256(
        f"e2e:{pure_function.__name__}".encode("ascii")
    ).hexdigest()
    artifact = _make_artifact(pure_function)
    carrier = ProductionCarrierState.placeholder(
        candidate_id, manifest_sha256
    ).finalize(artifact)
    if corrupt:
        damaged = artifact[:-1] + bytes([artifact[-1] ^ 1])
        carrier = replace(
            carrier,
            handle=InlineArtifactHandle(
                "inline-artifact",
                carrier.handle.content_sha256,
                carrier.handle.size_bytes,
                damaged,
            ),
        )
    wrapper_type = _guard_wrapper_class() if guard_miss else FallbackOnlyWrapper
    return wrapper_type(
        candidate_id=candidate_id,
        original_callable=original_method,
        carrier=carrier,
        logical_schema=logical_schema,
        usage_context="projection",
    )


def _write_parquet_fixture(directory: str) -> dict[str, object]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    partitions = tuple(
        (
            float(index) - 8.0,
            float(index) + 0.25,
        )
        for index in range(_FIXTURE_PARTITION_COUNT)
    )
    for index, values in enumerate(partitions):
        pq.write_table(
            pa.table({"measurement": pa.array(values, type=pa.float64())}),
            root / f"part-{index}.parquet",
        )
    pq.write_table(
        pa.table({"measurement": pa.array([], type=pa.float64())}),
        root / "empty.parquet",
    )
    return {
        "hostname": socket.gethostname(),
        "file_count": len(partitions) + 1,
    }


def _cleanup_fixture(directory: str) -> str:
    shutil.rmtree(directory, ignore_errors=True)
    return socket.gethostname()


def _collect_side_effects(run_id: str, scenario: str) -> dict[str, object]:
    count = 0
    paths = glob.glob(f"/tmp/udfjit-{run_id}-{scenario}-*.count")
    for value in paths:
        path = Path(value)
        try:
            count += len(path.read_bytes().splitlines())
        finally:
            path.unlink(missing_ok=True)
    return {"hostname": socket.gethostname(), "count": count}


def _node_tasks(function, worker_nodes: list[dict[str, Any]], *args):
    import ray
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    remote = ray.remote(num_cpus=1)(function)
    refs = [
        remote.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                node_id=node["NodeID"], soft=False
            )
        ).remote(*args)
        for node in worker_nodes
    ]
    return ray.get(refs)


def _side_effect_count(worker_nodes: list[dict[str, Any]], scenario: str) -> int:
    reports = _node_tasks(
        _collect_side_effects,
        worker_nodes,
        os.environ["UDFJIT_RUN_ID"],
        scenario,
    )
    return sum(int(report["count"]) for report in reports)


def _hook_types():
    from daft.dataframe.dataframe import DataFrame
    from daft.udf.udf_v2 import Func

    return Func, DataFrame


def _uninstall_hooks() -> None:
    from python_udf_jit.integration.daft_ray.control import uninstall_daft_control_hooks

    func_type, dataframe_type = _hook_types()
    uninstall_daft_control_hooks(func_type, dataframe_type)


def _install_hooks() -> None:
    import daft

    from python_udf_jit.integration.daft_ray.control import install_default_daft_hooks

    result = install_default_daft_hooks(daft)
    if result.status.value not in {"installed", "already_installed"}:
        raise AssertionError(f"Daft hook unavailable: {result}")


def _assert_hooks_installed() -> None:
    func_type, dataframe_type = _hook_types()
    marker = "__python_udf_jit_u2_hook__"
    if not (
        getattr(func_type.__call__, marker, False)
        and getattr(dataframe_type.with_columns, marker, False)
    ):
        raise AssertionError("Daft hooks were not installed by process bootstrap")


def _input_frame(path: str, *, empty: bool = False):
    import daft

    pattern = f"{path}/empty.parquet" if empty else f"{path}/part-*.parquet"
    return daft.read_parquet(pattern)


def _original_job(
    path: str,
    function: Callable[[float], float],
    *,
    empty: bool = False,
    method_override: Callable[..., float] | None = None,
):
    import daft

    _uninstall_hooks()
    try:
        frame = _input_frame(path, empty=empty)
        udf = daft.func(function)
        original_method = udf._method
        if method_override is not None:
            udf._method = method_override
        try:
            expression = udf(daft.col("measurement"))
        finally:
            udf._method = original_method
        return (
            frame.with_column("result", expression)
            .select("measurement", "result")
            .to_pydict()
        )
    finally:
        _install_hooks()


def _normal_auto_job(path: str, function: Callable[[float], float], *, empty: bool = False):
    import daft

    _install_hooks()
    frame = _input_frame(path, empty=empty)
    udf = daft.func(function)
    return (
        frame.with_column("result", udf(daft.col("measurement")))
        .select("measurement", "result")
        .to_pydict()
    )


def _plain_job(
    path: str,
    function: Callable[[float], float],
    *,
    empty: bool = False,
    method_override: Callable[..., float] | None = None,
):
    """Build the unmodified Daft expression in a real mode=off Ray Job."""

    import daft

    frame = _input_frame(path, empty=empty)
    udf = daft.func(function)
    original_method = udf._method
    if method_override is not None:
        udf._method = method_override
    try:
        expression = udf(daft.col("measurement"))
    finally:
        udf._method = original_method
    return (
        frame.with_column("result", expression)
        .select("measurement", "result")
        .to_pydict()
    )


def _diagnostic_job(path: str, function: Callable[[float], float]):
    import daft

    started_ns = time.time_ns()
    _assert_hooks_installed()
    warm_frame = _input_frame(path)
    warm_udf = daft.func(function)
    (
        warm_frame
        .with_column(
            "result",
            warm_udf(daft.col("measurement")),
        )
        .select("measurement", "result")
        .to_pydict()
    )
    frame = _input_frame(path)
    udf = daft.func(function)
    document = (
        frame.with_column(
            "result",
            udf(daft.col("measurement")),
        )
        .select("measurement", "result")
        .to_pydict()
    )
    return document, _runtime_events_since(
        path,
        started_ns=started_ns,
    )


def _runtime_events_since(
    path: str,
    *,
    started_ns: int,
) -> list[dict[str, object]]:
    """Read Worker-local runtime events without instrumenting the probe UDF."""

    import daft

    _uninstall_hooks()
    try:
        probe = daft.func(_diagnostic_probe_factory(started_ns))
        document = (
            _input_frame(path)
            .with_column("evidence", probe(daft.col("measurement")))
            .select("evidence")
            .to_pydict()
        )
    finally:
        _install_hooks()
    return _extract_events(document["evidence"])


def _zero_row_runtime_counts(
    events: list[dict[str, object]],
) -> dict[str, int]:
    return {
        "descriptor_count": sum(
            event.get("decision") == "descriptor_bound"
            for event in events
        ),
        "compile_count": sum(
            event.get("decision") == "compile" for event in events
        ),
        "hit_count": sum(
            event.get("decision") == "hit" for event in events
        ),
        "activity_event_count": len(events),
    }


def _custom_wrapper_job(path: str, wrapper: Any):
    import daft

    started_ns = time.time_ns()
    _uninstall_hooks()
    try:
        frame = _input_frame(path)
        udf = daft.func(_supported)
        original_method = udf._method
        udf._method = wrapper
        try:
            expression = udf(daft.col("measurement"))
        finally:
            udf._method = original_method
        frame = frame.with_column("result", expression)
        probe = daft.func(_diagnostic_probe_factory(started_ns))
        frame = frame.with_column("evidence", probe(daft.col("result")))
        document = frame.select("measurement", "result", "evidence").to_pydict()
    finally:
        _install_hooks()
    return document, _extract_events(document["evidence"])


def _extract_events(values: list[str]) -> list[dict[str, object]]:
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for value in values:
        report = json.loads(value)
        runtime_identity = (
            str(report["runtime_node_id"]),
            str(report["runtime_actor_id"]),
            str(report["runtime_worker_id"]),
            int(report["pid"]),
        )
        runtime_probe_task_id = str(report["runtime_task_id"])
        if not all(runtime_identity[:3]) or runtime_identity[3] <= 0:
            raise RuntimeError("ray_runtime_identity_incomplete")
        for raw_event in report["events"]:
            event = dict(raw_event)
            if (
                str(event["node_id"]) != runtime_identity[0]
                or str(event["actor_id"]) != runtime_identity[1]
                or int(event["pid"]) != runtime_identity[3]
            ):
                raise RuntimeError("runtime_event_process_identity_drift")
            event["worker_id"] = runtime_identity[2]
            event["runtime_probe_task_id"] = runtime_probe_task_id
            key = (
                event["timestamp_ns"],
                event["pid"],
                event["stage"],
                event["decision"],
                event["variant_key"],
            )
            unique[key] = event
    return sorted(unique.values(), key=lambda event: int(event["timestamp_ns"]))


def _fingerprint_mismatch_job(path: str) -> tuple[dict[str, list[Any]], str]:
    import daft
    from daft.dataframe.dataframe import DataFrame
    from daft.expressions.expressions import Expression
    from daft.udf.udf_v2 import Func

    from python_udf_jit.integration.daft_ray.compatibility import target_for_objects
    from python_udf_jit.integration.daft_ray.control import install_daft_control_hooks
    from python_udf_jit.integration.daft_ray.registry import CandidateRegistry

    _uninstall_hooks()
    target = target_for_objects(daft, Func, DataFrame)._replace(
        with_columns_fingerprint="0" * 64
    )
    try:
        result = install_daft_control_hooks(
            daft_module=daft,
            func_class=Func,
            dataframe_class=DataFrame,
            expression_class=Expression,
            mode="auto",
            registry=CandidateRegistry("a" * 64),
            target=target,
        )
        if result.status.value != "incompatible":
            raise AssertionError(result)
        frame = _input_frame(path)
        udf = daft.func(_supported)
        document = (
            frame.with_column("result", udf(daft.col("measurement")))
            .select("measurement", "result")
            .to_pydict()
        )
        return document, result.reason
    finally:
        _uninstall_hooks()
        _install_hooks()


def _event_documents(
    events: list[dict[str, object]],
    *,
    scenario: str,
    roles: dict[str, str],
) -> list[dict[str, object]]:
    return [
        {
            "event_type": "runtime",
            "phase": "e2e",
            "scenario": scenario,
            "stage": event["stage"],
            "decision": event["decision"],
            "reason_code": event["reason_code"],
            "cluster_epoch": event["cluster_epoch"],
            "run_id": event["run_id"],
            "role": roles.get(str(event["node_id"]), "unknown"),
            "node_id": event["node_id"],
            "actor_id": event["actor_id"],
            "worker_id": event.get("worker_id", ""),
            "pid": event["pid"],
            "process_generation": event["process_generation"],
            "partition_id": event["partition_id"],
            "task_attempt": event["task_attempt"],
            "runtime_probe_task_id": event.get("runtime_probe_task_id", ""),
            "variant_key": event["variant_key"],
            "artifact_hash": event["artifact_hash"],
            "code_hash": event["code_hash"],
            "execution_mode": event["execution_mode"],
            "timestamp_ns": event["timestamp_ns"],
        }
        for event in events
    ]


def _state_value(record: Any, name: str, default: object = "") -> object:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _state_text(record: Any, name: str) -> str:
    value = _state_value(record, name)
    if value is None:
        return ""
    text = str(value)
    return "" if text == "None" else text


def _state_int(record: Any, name: str, default: int = -1) -> int:
    value = _state_value(record, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _task_state_document(record: Any) -> dict[str, object]:
    return {
        "task_id": _state_text(record, "task_id"),
        "attempt_number": _state_int(record, "attempt_number"),
        "state": _state_text(record, "state"),
        "type": _state_text(record, "type"),
        "actor_id": _state_text(record, "actor_id"),
        "node_id": _state_text(record, "node_id"),
        "worker_id": _state_text(record, "worker_id"),
        "worker_pid": _state_int(record, "worker_pid"),
        "name": _state_text(record, "name"),
        "parent_task_id": _state_text(record, "parent_task_id"),
        "start_time_ms": _state_int(record, "start_time_ms"),
        "end_time_ms": _state_int(record, "end_time_ms"),
    }


def _state_identity_matches(
    record: Any,
    event: dict[str, object],
) -> bool:
    try:
        worker_pid = int(event.get("pid", -1))
    except (TypeError, ValueError):
        return False
    return (
        bool(_state_text(record, "actor_id"))
        and _state_text(record, "actor_id") == str(event.get("actor_id", ""))
        and _state_text(record, "node_id") == str(event.get("node_id", ""))
        and _state_text(record, "worker_id") == str(event.get("worker_id", ""))
        and _state_int(record, "worker_pid") == worker_pid
    )


def _temporal_task_candidates(
    event: dict[str, object],
    records: list[Any],
) -> list[Any]:
    try:
        timestamp_ms = int(event.get("timestamp_ns", 0)) / 1_000_000
    except (TypeError, ValueError):
        return []
    if timestamp_ms <= 0:
        return []
    candidates = []
    for record in records:
        start_ms = _state_int(record, "start_time_ms")
        end_ms = _state_int(record, "end_time_ms")
        if (
            _state_text(record, "state") == "FINISHED"
            and _state_text(record, "type") == "ACTOR_TASK"
            and _state_identity_matches(record, event)
            and start_ms > 0
            and end_ms >= start_ms
            and start_ms <= timestamp_ms <= end_ms + 1
        ):
            candidates.append(record)
    return candidates


def _state_join_pending(
    events: list[dict[str, object]],
    records: list[Any],
    records_by_id: dict[str, list[Any]],
) -> bool:
    for event in events:
        runtime_task_id = str(event.get("partition_id", ""))
        exact_records = records_by_id.get(runtime_task_id, [])
        if exact_records:
            if len(exact_records) == 1 and _state_text(
                exact_records[0], "state"
            ) not in {"FINISHED", "FAILED"}:
                return True
            continue
        temporal = _temporal_task_candidates(event, records)
        if not temporal:
            return True
        if len(temporal) == 1:
            attempts = records_by_id.get(_state_text(temporal[0], "task_id"), [])
            if len(attempts) == 1 and _state_text(
                attempts[0], "state"
            ) not in {"FINISHED", "FAILED"}:
                return True
    return False


def _join_ray_task_attempts(
    events: list[dict[str, object]],
    *,
    wait_seconds: float = 20.0,
    poll_seconds: float = 0.25,
) -> list[dict[str, object]]:
    """Join Worker task IDs to the authoritative Ray State attempt records."""

    from ray.util.state import list_tasks

    state_records: list[Any] = []
    records_by_id: dict[str, list[Any]] = {}
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while events:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        query_timeout = min(30, int(remaining))
        if query_timeout < 1:
            break
        state_records = list(
            list_tasks(
                detail=True,
                limit=10000,
                timeout=query_timeout,
            )
        )
        records_by_id = {}
        for record in state_records:
            task_id = _state_text(record, "task_id")
            if task_id:
                records_by_id.setdefault(task_id, []).append(record)
        pending = _state_join_pending(events, state_records, records_by_id)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        time.sleep(min(max(0.0, poll_seconds), remaining))

    joined = []
    for event in events:
        item = dict(event)
        runtime_task_id = str(item.get("partition_id", ""))
        exact_records = records_by_id.get(runtime_task_id, [])
        temporal_candidates = _temporal_task_candidates(item, state_records)
        attempt_records = exact_records
        strategy = "runtime_task_id" if exact_records else ""
        if not exact_records and len(temporal_candidates) == 1:
            authoritative_task_id = _state_text(
                temporal_candidates[0],
                "task_id",
            )
            attempt_records = records_by_id.get(authoritative_task_id, [])
            strategy = "unique_identity_time_window"
        item["runtime_context_task_id"] = runtime_task_id
        item["ray_state_join_strategy"] = strategy
        item["ray_state_exact_records"] = [
            _task_state_document(record) for record in exact_records
        ]
        item["ray_state_temporal_candidates"] = [
            _task_state_document(record) for record in temporal_candidates
        ]
        if not exact_records:
            actor_id = str(item.get("actor_id", ""))
            node_id = str(item.get("node_id", ""))
            try:
                worker_pid = int(item.get("pid", -1))
            except (TypeError, ValueError):
                worker_pid = -1
            item["ray_state_identity_candidates"] = [
                _task_state_document(record)
                for record in state_records
                if (
                    _state_text(record, "actor_id") == actor_id
                    and _state_text(record, "node_id") == node_id
                    and _state_int(record, "worker_pid") == worker_pid
                )
            ][-10:]
        item["ray_state_attempt_records"] = [
            _task_state_document(record) for record in attempt_records
        ]
        if len(attempt_records) == 1:
            record = attempt_records[0]
            identity_matches = (
                _state_text(record, "state") == "FINISHED"
                and _state_identity_matches(record, item)
            )
            attempt_number = _state_int(record, "attempt_number")
            if identity_matches and attempt_number == 0:
                item["partition_id"] = _state_text(record, "task_id")
                item["task_attempt"] = "attempt-0"
        joined.append(item)
    return joined


def run_live_job() -> dict[str, object]:
    import daft
    import ray

    from python_udf_jit.compiler.capture import CaptureRequest, try_capture

    job_mode = os.environ.get("UDFJIT_MODE", "")
    if job_mode not in {"off", "auto"}:
        raise RuntimeError("Ray Job requires UDFJIT_MODE=off or auto")
    run_id = os.environ["UDFJIT_RUN_ID"]
    cluster_epoch = os.environ["UDFJIT_CLUSTER_EPOCH"]
    manifest_path = Path(os.environ["UDFJIT_MANIFEST_PATH"])
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    fixture_path = f"/tmp/python-udf-jit-e2e-{run_id}-{job_mode}"

    ray.init(address="auto")
    daft.set_runner_ray(address="auto", noop_if_initialized=True)
    daft.set_execution_config(
        max_sources_per_scan_task=_SOURCES_PER_SCAN_TASK,
        min_cpu_per_task=_MIN_CPU_PER_TASK,
    )
    alive = [node for node in ray.nodes() if node.get("Alive")]
    heads = [node for node in alive if node.get("NodeName") == "ray-head-driver"]
    workers = sorted(
        [node for node in alive if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}],
        key=lambda node: node["NodeName"],
    )
    if len(heads) != 1 or len(workers) != 2 or heads[0]["Resources"].get("CPU", 0) != 0:
        raise RuntimeError("three-node topology drift")
    driver_node_id = ray.get_runtime_context().get_node_id()
    if driver_node_id != heads[0]["NodeID"]:
        raise RuntimeError("Ray Jobs Driver did not start on Head")
    roles = {node["NodeID"]: node["NodeName"] for node in alive}

    _write_parquet_fixture(fixture_path)
    _node_tasks(_write_parquet_fixture, workers, fixture_path)
    raw_events: list[dict[str, object]] = []
    try:
        if job_mode == "off":
            off_supported = _plain_job(fixture_path, _supported)
            _side_effect_count(workers, "guard")
            off_guard = _plain_job(
                fixture_path,
                _guard_value,
                method_override=_guard_side_effect_method,
            )
            off_guard_calls = _side_effect_count(workers, "guard")
            _side_effect_count(workers, "unsupported")
            off_unsupported = _plain_job(fixture_path, _unsupported_side_effect)
            off_unsupported_calls = _side_effect_count(workers, "unsupported")
            _side_effect_count(workers, "corrupt")
            off_corrupt = _plain_job(
                fixture_path,
                _corrupt_value,
                method_override=_corrupt_side_effect_method,
            )
            off_corrupt_calls = _side_effect_count(workers, "corrupt")
            zero_off = _plain_job(fixture_path, _supported, empty=True)
            return {
                "schema_version": 1,
                "job_mode": "off",
                "run_id": run_id,
                "cluster_epoch": cluster_epoch,
                "driver": {
                    "node_id": driver_node_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "ray_job_id": os.environ.get("RAY_JOB_ID", ""),
                    "ray_runtime_job_id": str(ray.get_runtime_context().get_job_id()),
                },
                "topology": {
                    "head_node_id": heads[0]["NodeID"],
                    "worker_node_ids": [node["NodeID"] for node in workers],
                    "roles": roles,
                },
                "manifest_sha256": manifest_sha256,
                "raw_events": [],
                "scenarios": {
                    "supported": {
                        "result_digest": _result_digest(off_supported),
                        "row_count": len(off_supported["measurement"]),
                    },
                    "guard_miss": {
                        "result_digest": _result_digest(off_guard),
                        "callable_calls": off_guard_calls,
                    },
                    "unsupported": {
                        "result_digest": _result_digest(off_unsupported),
                        "callable_calls": off_unsupported_calls,
                    },
                    "mode_off": {
                        "completed": True,
                        "result_digest": _result_digest(off_supported),
                    },
                    "corrupt_artifact": {
                        "result_digest": _result_digest(off_corrupt),
                        "callable_calls": off_corrupt_calls,
                    },
                    "zero_row": {
                        "result_digest": _result_digest(zero_off),
                        "row_count": len(zero_off["measurement"]),
                    },
                },
            }

        auto_supported, supported_events = _diagnostic_job(fixture_path, _supported)
        raw_events.extend(
            _event_documents(supported_events, scenario="supported", roles=roles)
        )

        _side_effect_count(workers, "guard")
        guard_frame = _input_frame(fixture_path)
        guard_wrapper = _artifact_wrapper(
            pure_function=_guard_value,
            original_method=_guard_side_effect_method,
            logical_schema=repr(guard_frame.schema()),
            manifest_sha256=manifest_sha256,
            guard_miss=True,
        )
        auto_guard, guard_events = _custom_wrapper_job(fixture_path, guard_wrapper)
        auto_guard_calls = _side_effect_count(workers, "guard")
        raw_events.extend(
            _event_documents(guard_events, scenario="guard_miss", roles=roles)
        )

        _side_effect_count(workers, "unsupported")
        auto_unsupported = _normal_auto_job(fixture_path, _unsupported_side_effect)
        auto_unsupported_calls = _side_effect_count(workers, "unsupported")
        capture_result = try_capture(CaptureRequest(_unsupported_side_effect))
        reject_code = (
            capture_result.reject_code.value if capture_result.reject_code is not None else ""
        )

        fingerprint, fingerprint_reason = _fingerprint_mismatch_job(fixture_path)

        _side_effect_count(workers, "corrupt")
        corrupt_frame = _input_frame(fixture_path)
        corrupt_wrapper = _artifact_wrapper(
            pure_function=_corrupt_value,
            original_method=_corrupt_side_effect_method,
            logical_schema=repr(corrupt_frame.schema()),
            manifest_sha256=manifest_sha256,
            corrupt=True,
        )
        corrupt, corrupt_events = _custom_wrapper_job(fixture_path, corrupt_wrapper)
        corrupt_calls = _side_effect_count(workers, "corrupt")
        raw_events.extend(
            _event_documents(corrupt_events, scenario="corrupt_artifact", roles=roles)
        )
        zero_started_ns = time.time_ns()
        zero_auto = _normal_auto_job(fixture_path, _supported, empty=True)
        zero_events = _runtime_events_since(
            fixture_path,
            started_ns=zero_started_ns,
        )
        zero_counts = _zero_row_runtime_counts(zero_events)
        raw_events.extend(
            _event_documents(zero_events, scenario="zero_row", roles=roles)
        )
        raw_events = _join_ray_task_attempts(raw_events)
        driver_process_generation = f"driver-{os.getpid()}"
        driver_timestamp = time.time_ns()
        for index, (scenario, decision, reason_code) in enumerate(
            (
                ("unsupported", "rejected", reject_code),
                (
                    "fingerprint_mismatch",
                    "fail_open",
                    fingerprint_reason,
                ),
            )
        ):
            raw_events.append(
                {
                    "event_type": "driver",
                    "phase": "e2e",
                    "scenario": scenario,
                    "stage": "capture" if decision == "rejected" else "adapter",
                    "decision": decision,
                    "reason_code": reason_code,
                    "cluster_epoch": cluster_epoch,
                    "run_id": run_id,
                    "role": "ray-head-driver",
                    "node_id": driver_node_id,
                    "actor_id": "",
                    "worker_id": "",
                    "pid": os.getpid(),
                    "process_generation": driver_process_generation,
                    "partition_id": "",
                    "task_attempt": "",
                    "variant_key": "",
                    "artifact_hash": "",
                    "code_hash": "",
                    "execution_mode": "",
                    "timestamp_ns": driver_timestamp + index,
                }
            )

        row_count = len(auto_supported["measurement"])
        return {
            "schema_version": 1,
            "job_mode": "auto",
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "driver": {
                "node_id": driver_node_id,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "ray_job_id": os.environ.get("RAY_JOB_ID", ""),
                "ray_runtime_job_id": str(ray.get_runtime_context().get_job_id()),
            },
            "topology": {
                "head_node_id": heads[0]["NodeID"],
                "worker_node_ids": [node["NodeID"] for node in workers],
                "roles": roles,
            },
            "manifest_sha256": manifest_sha256,
            "raw_events": raw_events,
            "scenarios": {
                "supported": {
                    "result_digest": _result_digest(auto_supported),
                    "row_count": row_count,
                    "callable_calls": 0,
                    "side_effect_count": 0,
                },
                "guard_miss": {
                    "result_digest": _result_digest(auto_guard),
                    "row_count": row_count,
                    "callable_calls": auto_guard_calls,
                    "fallback_count": sum(
                        event["decision"] == "fallback" for event in guard_events
                    ),
                    "side_effect_count": auto_guard_calls,
                    "semantic_execute_count": sum(
                        event["decision"] == "semantic_execute" for event in guard_events
                    ),
                    "reason_code": "schema_mismatch",
                },
                "unsupported": {
                    "result_digest": _result_digest(auto_unsupported),
                    "row_count": row_count,
                    "callable_calls": auto_unsupported_calls,
                    "side_effect_count": auto_unsupported_calls,
                    "reason_code": reject_code,
                },
                "fingerprint_mismatch": {
                    "completed": True,
                    "result_digest": _result_digest(fingerprint),
                    "reason_code": fingerprint_reason,
                },
                "corrupt_artifact": {
                    "completed": True,
                    "result_digest": _result_digest(corrupt),
                    "reason_code": "artifact_mismatch",
                    "callable_calls": corrupt_calls,
                },
                "zero_row": {
                    "result_digest": _result_digest(zero_auto),
                    "row_count": len(zero_auto["measurement"]),
                    "callable_calls": 0,
                    **zero_counts,
                },
            },
        }
    finally:
        _cleanup_fixture(fixture_path)
        _node_tasks(_cleanup_fixture, workers, fixture_path)
        ray.shutdown()


def _write_output(path: Path, document: dict[str, object]) -> None:
    write_private_json(path, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    _write_output(arguments.output, run_live_job())
    print(arguments.output)


if __name__ == "__main__":
    main()
