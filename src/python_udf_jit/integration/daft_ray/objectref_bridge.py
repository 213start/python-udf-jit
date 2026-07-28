from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import os
import threading
from dataclasses import dataclass
from typing import Any

from python_udf_jit.integration.daft_ray.compatibility import (
    callable_fingerprint,
)
from python_udf_jit.protocol.loader import (
    clear_prefetched_artifact_payloads,
    prefetch_artifact_payload,
)
from python_udf_jit.protocol.manifest import DEFAULT_MANIFEST


_BRIDGE_CONTEXT_KEY = "__python_udf_jit_artifacts__"
_BRIDGE_PARTITION_SET_KEY = "__python_udf_jit_artifact_references__"
_BRIDGE_MARKER = "__python_udf_jit_objectref_bridge__"
_ORIGINAL_METHOD = "__python_udf_jit_objectref_original__"
_INSTALL_LOCK = threading.RLock()
_DRIVER_LOCK = threading.RLock()
_SCHEDULER_LOCK = threading.RLock()
_DRIVER_ARTIFACTS: dict[str, "DriverArtifactReference"] = {}
_SCHEDULER_PLAN_ARTIFACTS: dict[
    str,
    tuple["DriverArtifactReference", ...],
] = {}
_MAX_DRIVER_ARTIFACTS = 1024
_MAX_SCHEDULER_PLANS = 1024


@dataclass(frozen=True)
class DriverArtifactReference:
    content_sha256: str
    size_bytes: int
    reference: object

    def __post_init__(self) -> None:
        if (
            type(self.content_sha256) is not str
            or len(self.content_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.content_sha256
            )
            or type(self.size_bytes) is not int
            or self.size_bytes <= 0
            or self.size_bytes > DEFAULT_MANIFEST.max_total_bytes
            or self.reference is None
        ):
            raise ValueError("invalid Driver artifact reference")


@dataclass(frozen=True)
class ObjectRefBridgeTarget:
    run_plan_signature: tuple[tuple[str, str], ...]
    submit_task_signature: tuple[tuple[str, str], ...]
    stream_plan_signature: tuple[tuple[str, str], ...]
    scheduler_run_plan_signature: tuple[tuple[str, str], ...]
    scheduler_get_next_signature: tuple[tuple[str, str], ...]
    run_plan_fingerprint: str
    submit_task_fingerprint: str
    stream_plan_fingerprint: str
    scheduler_run_plan_fingerprint: str
    scheduler_get_next_fingerprint: str


@dataclass(frozen=True)
class ObjectRefBridgeResult:
    installed: bool
    reason: str


DAFT_V0_7_2_OBJECTREF_TARGET = ObjectRefBridgeTarget(
    run_plan_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("plan", "POSITIONAL_OR_KEYWORD"),
        ("exec_cfg", "POSITIONAL_OR_KEYWORD"),
        ("psets", "POSITIONAL_OR_KEYWORD"),
        ("context", "POSITIONAL_OR_KEYWORD"),
    ),
    submit_task_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("task", "POSITIONAL_OR_KEYWORD"),
    ),
    stream_plan_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("plan", "POSITIONAL_OR_KEYWORD"),
        ("partition_sets", "POSITIONAL_OR_KEYWORD"),
    ),
    scheduler_run_plan_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("plan", "POSITIONAL_OR_KEYWORD"),
        ("partition_sets", "POSITIONAL_OR_KEYWORD"),
        ("_ray_trace_ctx", "KEYWORD_ONLY"),
    ),
    scheduler_get_next_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("plan_id", "POSITIONAL_OR_KEYWORD"),
        ("_ray_trace_ctx", "KEYWORD_ONLY"),
    ),
    run_plan_fingerprint=(
        "5edd09dfa1c01d6d674f972f96f1c303a"
        "0958967a0d5c3f2d2641aa8d6116d67"
    ),
    submit_task_fingerprint=(
        "9ead1d6619a29b6e277d8803ac000fb7"
        "1089447579a75af5e53a2ea37835ac30"
    ),
    stream_plan_fingerprint=(
        "a9b89345a94266236fa746626621f53c5"
        "bc60ea17a9fcd4c885f908e48741b13"
    ),
    scheduler_run_plan_fingerprint=(
        "d25e04c37eb5ad1c65f1ea5e2b55209"
        "fa5a185508c5d4f1b77318eb284de9f6a"
    ),
    scheduler_get_next_fingerprint=(
        "594803324d56ca75a03603af3306a393e"
        "85d02d119f855d1bcbfd31ede26b1ab"
    ),
)


def _signature_shape(
    callable_object: Any,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (parameter.name, parameter.kind.name)
        for parameter in inspect.signature(
            callable_object
        ).parameters.values()
    )


def target_for_objectref_bridge(
    run_plan: Any,
    submit_task: Any,
    stream_plan: Any,
    scheduler_run_plan: Any,
    scheduler_get_next: Any,
) -> ObjectRefBridgeTarget:
    return ObjectRefBridgeTarget(
        _signature_shape(run_plan),
        _signature_shape(submit_task),
        _signature_shape(stream_plan),
        _signature_shape(scheduler_run_plan),
        _signature_shape(scheduler_get_next),
        callable_fingerprint(run_plan),
        callable_fingerprint(submit_task),
        callable_fingerprint(stream_plan),
        callable_fingerprint(scheduler_run_plan),
        callable_fingerprint(scheduler_get_next),
    )


def register_driver_artifact_reference(
    payload: bytes,
    reference: object,
) -> object:
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > DEFAULT_MANIFEST.max_total_bytes
        or reference is None
    ):
        raise ValueError("invalid Driver artifact reference")
    content_sha256 = hashlib.sha256(payload).hexdigest()
    record = DriverArtifactReference(
        content_sha256,
        len(payload),
        reference,
    )
    with _DRIVER_LOCK:
        existing = _DRIVER_ARTIFACTS.get(content_sha256)
        if (
            existing is not None
            and existing.size_bytes != record.size_bytes
        ):
            raise ValueError("Driver artifact reference collision")
        _DRIVER_ARTIFACTS[content_sha256] = record
        while len(_DRIVER_ARTIFACTS) > _MAX_DRIVER_ARTIFACTS:
            oldest = next(iter(_DRIVER_ARTIFACTS))
            _DRIVER_ARTIFACTS.pop(oldest)
    return reference


def driver_artifact_references() -> tuple[DriverArtifactReference, ...]:
    with _DRIVER_LOCK, _SCHEDULER_LOCK:
        records: dict[str, DriverArtifactReference] = dict(
            _DRIVER_ARTIFACTS
        )
        for active in _SCHEDULER_PLAN_ARTIFACTS.values():
            for record in active:
                records.setdefault(
                    record.content_sha256,
                    record,
                )
        return tuple(records.values())


def clear_driver_artifact_references() -> None:
    with _DRIVER_LOCK, _SCHEDULER_LOCK:
        _DRIVER_ARTIFACTS.clear()
        _SCHEDULER_PLAN_ARTIFACTS.clear()


def scheduler_artifact_references() -> tuple[
    DriverArtifactReference,
    ...,
]:
    with _SCHEDULER_LOCK:
        records: dict[str, DriverArtifactReference] = {}
        for active in _SCHEDULER_PLAN_ARTIFACTS.values():
            for record in active:
                records.setdefault(
                    record.content_sha256,
                    record,
                )
        return tuple(records.values())


def _register_scheduler_plan_artifacts(
    plan_id: str,
    records: tuple[DriverArtifactReference, ...],
) -> None:
    if (
        type(plan_id) is not str
        or not plan_id
        or len(plan_id.encode("utf-8")) > 4096
        or not records
        or len(records) > _MAX_DRIVER_ARTIFACTS
        or not all(
            isinstance(record, DriverArtifactReference)
            for record in records
        )
    ):
        raise ValueError("invalid Scheduler artifact references")
    with _SCHEDULER_LOCK:
        if (
            plan_id not in _SCHEDULER_PLAN_ARTIFACTS
            and len(_SCHEDULER_PLAN_ARTIFACTS)
            >= _MAX_SCHEDULER_PLANS
        ):
            raise ValueError("Scheduler artifact plan capacity exceeded")
        _SCHEDULER_PLAN_ARTIFACTS[plan_id] = records


def _clear_scheduler_plan_artifacts(plan_id: str) -> None:
    with _SCHEDULER_LOCK:
        _SCHEDULER_PLAN_ARTIFACTS.pop(plan_id, None)


def install_daft_objectref_bridge(
    flotilla_module: Any,
    *,
    target: ObjectRefBridgeTarget = (
        DAFT_V0_7_2_OBJECTREF_TARGET
    ),
) -> ObjectRefBridgeResult:
    with _INSTALL_LOCK:
        try:
            actor_class = flotilla_module.RaySwordfishActor
            modified_class = (
                actor_class.__ray_metadata__.modified_class
            )
            run_plan = modified_class.run_plan
            handle_class = flotilla_module.RaySwordfishActorHandle
            submit_task = handle_class.submit_task
            runner_class = flotilla_module.FlotillaRunner
            stream_plan = runner_class.stream_plan
            scheduler_class = (
                flotilla_module.RemoteFlotillaRunner
                .__ray_metadata__.modified_class
            )
            scheduler_run_plan = scheduler_class.run_plan
            scheduler_get_next = (
                scheduler_class.get_next_partition
            )
        except Exception as error:
            return ObjectRefBridgeResult(
                False,
                f"objectref_bridge_surface_missing:{type(error).__name__}",
            )

        installed_states = tuple(
            bool(getattr(method, _BRIDGE_MARKER, False))
            for method in (
                run_plan,
                submit_task,
                stream_plan,
                scheduler_run_plan,
                scheduler_get_next,
            )
        )
        if all(installed_states):
            return ObjectRefBridgeResult(
                True,
                "objectref_bridge_already_installed",
            )
        if any(installed_states):
            return ObjectRefBridgeResult(
                False,
                "objectref_bridge_partial_state",
            )
        try:
            actual = target_for_objectref_bridge(
                run_plan,
                submit_task,
                stream_plan,
                scheduler_run_plan,
                scheduler_get_next,
            )
        except Exception as error:
            return ObjectRefBridgeResult(
                False,
                f"objectref_bridge_fingerprint_unavailable:{type(error).__name__}",
            )
        if actual != target:
            return ObjectRefBridgeResult(
                False,
                "objectref_bridge_contract_mismatch",
            )

        @functools.wraps(run_plan)
        async def wrapped_run_plan(
            self,
            plan,
            exec_cfg,
            psets,
            context,
        ):
            bridge_value = None
            execution_context = context
            if isinstance(context, dict):
                execution_context = dict(context)
                bridge_value = execution_context.pop(
                    _BRIDGE_CONTEXT_KEY,
                    None,
                )
            job_namespace = os.environ.get(
                "UDFJIT_RUN_ID",
                os.environ.get(
                    "UDFJIT_CLUSTER_EPOCH",
                    "default-ray-job",
                ),
            )
            tenant_namespace = os.environ.get(
                "UDFJIT_TENANT_NAMESPACE",
                "default",
            )
            prefetched: list[str] = []
            try:
                if (
                    isinstance(bridge_value, tuple)
                    and len(bridge_value) == 2
                    and type(bridge_value[0]) is bool
                    and isinstance(bridge_value[1], tuple)
                    and all(
                        isinstance(
                            item,
                            DriverArtifactReference,
                        )
                        for item in bridge_value[1]
                    )
                ):
                    context_was_none, records = bridge_value
                    async def resolve_reference(
                        value: object,
                    ) -> object:
                        if isinstance(value, bytes):
                            return value
                        return await value  # type: ignore[misc]

                    resolved = await asyncio.gather(
                        *(
                            resolve_reference(
                                record.reference
                            )
                            for record in records
                        ),
                        return_exceptions=True,
                    )
                    for record, payload in zip(
                        records,
                        resolved,
                    ):
                        if (
                            isinstance(payload, bytes)
                            and len(payload) == record.size_bytes
                        ):
                            try:
                                prefetch_artifact_payload(
                                    job_namespace=job_namespace,
                                    tenant_namespace=tenant_namespace,
                                    content_sha256=(
                                        record.content_sha256
                                    ),
                                    payload=payload,
                                )
                            except ValueError:
                                continue
                            prefetched.append(
                                record.content_sha256
                            )
                    if context_was_none and not execution_context:
                        execution_context = None
                async for item in run_plan(
                    self,
                    plan,
                    exec_cfg,
                    psets,
                    execution_context,
                ):
                    yield item
            finally:
                clear_prefetched_artifact_payloads(
                    job_namespace=job_namespace,
                    tenant_namespace=tenant_namespace,
                    content_sha256s=tuple(prefetched),
                )

        @functools.wraps(stream_plan)
        def wrapped_stream_plan(
            self,
            plan,
            partition_sets,
        ):
            references = driver_artifact_references()
            if (
                not references
                or not isinstance(partition_sets, dict)
                or _BRIDGE_PARTITION_SET_KEY in partition_sets
            ):
                yield from stream_plan(
                    self,
                    plan,
                    partition_sets,
                )
                return
            try:
                plan_id = plan.idx()
                if type(plan_id) is not str or not plan_id:
                    raise ValueError("invalid distributed plan id")
            except Exception:
                yield from stream_plan(
                    self,
                    plan,
                    partition_sets,
                )
                return
            bridged_partition_sets = dict(partition_sets)
            bridged_partition_sets[_BRIDGE_PARTITION_SET_KEY] = (
                plan_id,
                references,
            )
            yield from stream_plan(
                self,
                plan,
                bridged_partition_sets,
            )

        @functools.wraps(scheduler_run_plan)
        def wrapped_scheduler_run_plan(
            self,
            plan,
            partition_sets,
            *,
            _ray_trace_ctx=None,
        ):
            registered_plan_id = ""
            execution_partition_sets = partition_sets
            if (
                isinstance(partition_sets, dict)
                and _BRIDGE_PARTITION_SET_KEY
                in partition_sets
            ):
                bridge_value = partition_sets[
                    _BRIDGE_PARTITION_SET_KEY
                ]
                try:
                    plan_id = plan.idx()
                except Exception:
                    plan_id = None
                if (
                    isinstance(bridge_value, tuple)
                    and len(bridge_value) == 2
                    and type(bridge_value[0]) is str
                    and bridge_value[0] == plan_id
                    and isinstance(bridge_value[1], tuple)
                    and bridge_value[1]
                    and all(
                        isinstance(
                            item,
                            DriverArtifactReference,
                        )
                        for item in bridge_value[1]
                    )
                ):
                    execution_partition_sets = dict(
                        partition_sets
                    )
                    execution_partition_sets.pop(
                        _BRIDGE_PARTITION_SET_KEY
                    )
                    _register_scheduler_plan_artifacts(
                        bridge_value[0],
                        bridge_value[1],
                    )
                    registered_plan_id = bridge_value[0]
            try:
                return scheduler_run_plan(
                    self,
                    plan,
                    execution_partition_sets,
                    _ray_trace_ctx=_ray_trace_ctx,
                )
            except BaseException:
                if registered_plan_id:
                    _clear_scheduler_plan_artifacts(
                        registered_plan_id
                    )
                raise

        @functools.wraps(scheduler_get_next)
        async def wrapped_scheduler_get_next(
            self,
            plan_id,
            *,
            _ray_trace_ctx=None,
        ):
            try:
                result = await scheduler_get_next(
                    self,
                    plan_id,
                    _ray_trace_ctx=_ray_trace_ctx,
                )
            except BaseException:
                _clear_scheduler_plan_artifacts(plan_id)
                raise
            if result is None:
                _clear_scheduler_plan_artifacts(plan_id)
            return result

        @functools.wraps(submit_task)
        def wrapped_submit_task(self, task):
            psets = {
                key: [
                    value.object_ref
                    for value in values
                ]
                for key, values in task.psets().items()
            }
            original_context = task.context()
            context = (
                {}
                if original_context is None
                else dict(original_context)
            )
            references = driver_artifact_references()
            if references:
                if _BRIDGE_CONTEXT_KEY in context:
                    return submit_task(self, task)
                context[_BRIDGE_CONTEXT_KEY] = (
                    original_context is None,
                    references,
                )
            result_handle = self.actor_handle.run_plan.options(
                name=task.name()
            ).remote(
                task.plan(),
                task.config(),
                psets,
                context if context else original_context,
            )
            return flotilla_module.RaySwordfishTaskHandle(
                result_handle,
                self.actor_handle,
            )

        setattr(wrapped_run_plan, _BRIDGE_MARKER, True)
        setattr(wrapped_run_plan, _ORIGINAL_METHOD, run_plan)
        setattr(wrapped_submit_task, _BRIDGE_MARKER, True)
        setattr(wrapped_submit_task, _ORIGINAL_METHOD, submit_task)
        setattr(wrapped_stream_plan, _BRIDGE_MARKER, True)
        setattr(wrapped_stream_plan, _ORIGINAL_METHOD, stream_plan)
        setattr(
            wrapped_scheduler_run_plan,
            _BRIDGE_MARKER,
            True,
        )
        setattr(
            wrapped_scheduler_run_plan,
            _ORIGINAL_METHOD,
            scheduler_run_plan,
        )
        setattr(
            wrapped_scheduler_get_next,
            _BRIDGE_MARKER,
            True,
        )
        setattr(
            wrapped_scheduler_get_next,
            _ORIGINAL_METHOD,
            scheduler_get_next,
        )
        try:
            modified_class.run_plan = wrapped_run_plan
            handle_class.submit_task = wrapped_submit_task
            runner_class.stream_plan = wrapped_stream_plan
            scheduler_class.run_plan = (
                wrapped_scheduler_run_plan
            )
            scheduler_class.get_next_partition = (
                wrapped_scheduler_get_next
            )
        except Exception:
            modified_class.run_plan = run_plan
            handle_class.submit_task = submit_task
            runner_class.stream_plan = stream_plan
            scheduler_class.run_plan = scheduler_run_plan
            scheduler_class.get_next_partition = (
                scheduler_get_next
            )
            return ObjectRefBridgeResult(
                False,
                "objectref_bridge_install_failed",
            )
        return ObjectRefBridgeResult(
            True,
            "objectref_bridge_installed",
        )
