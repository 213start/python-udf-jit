from __future__ import annotations

import hashlib
import os
import platform
import secrets
import sysconfig
import types
from dataclasses import dataclass
from typing import Any, Callable

from python_udf_jit.compiler.capture import CaptureRequest, FallbackIdentity, capture
from python_udf_jit.compiler.region import verify_semantic_region_graph
from python_udf_jit.compiler.verifier import (
    verify_semantic_module,
)
from python_udf_jit.diagnostics.report import (
    DEFAULT_RUNTIME_REPORT,
    RuntimeEvent,
    RuntimeEventSink,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.protocol.artifact import PortableUdfArtifact
from python_udf_jit.protocol.loader import (
    ArtifactLoadError,
    ArtifactLoader,
    ArtifactLoadRejectCode,
    LoaderNamespace,
)
from python_udf_jit.provider.scalar_python.executor import (
    CinderXScalarProviderFactory,
    PreSemanticsExecutionError,
    ScalarProviderFactory,
    ScalarProviderVariant,
)
from python_udf_jit.runtime.guards import (
    OuterGuardError,
    OuterGuardExpectation,
    OuterGuardObservation,
    OuterGuardRejectCode,
    guard_outer_entry,
)
from python_udf_jit.runtime.continuation import CommitBoundary
from python_udf_jit.runtime.layout import ProcessIdentity, SCALAR_SLOT_ABI_VERSION
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer
from python_udf_jit.runtime.variant import (
    CacheDecision,
    ProcessVariantCache,
    VariantKey,
    WorkerProcessKey,
)


_PROCESS_GENERATION = secrets.token_hex(16)
_PROCESS_ARTIFACT_LOADER = ArtifactLoader()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _runtime_context_value(context: Any, method: str) -> str:
    function = getattr(context, method, None)
    if not callable(function):
        return ""
    try:
        value = function()
    except Exception:
        return ""
    return "" if value is None else str(value)


@dataclass(frozen=True)
class WorkerRuntimeContext:
    run_id: str
    process: WorkerProcessKey
    partition_id: str = ""
    task_attempt: str = ""
    refresh_partition_from_ray: bool = False
    tenant_namespace: str = "default"

    @classmethod
    def from_environment(cls) -> "WorkerRuntimeContext":
        cluster_epoch = os.environ.get("UDFJIT_CLUSTER_EPOCH", "")
        run_id = os.environ.get("UDFJIT_RUN_ID", cluster_epoch)
        node_id = os.environ.get("UDFJIT_NODE_ID", "")
        actor_worker_id = os.environ.get("UDFJIT_ACTOR_WORKER_ID", "")
        partition_id = os.environ.get("UDFJIT_PARTITION_ID", "")
        refresh_partition_from_ray = not partition_id
        task_attempt = os.environ.get("UDFJIT_TASK_ATTEMPT", "")
        tenant_namespace = os.environ.get(
            "UDFJIT_TENANT_NAMESPACE",
            "default",
        )
        if not node_id or not actor_worker_id or not partition_id:
            try:
                import ray

                runtime = ray.get_runtime_context()
                node_id = node_id or _runtime_context_value(runtime, "get_node_id")
                actor_worker_id = (
                    actor_worker_id
                    or _runtime_context_value(runtime, "get_actor_id")
                    or _runtime_context_value(runtime, "get_worker_id")
                )
                partition_id = partition_id or _runtime_context_value(
                    runtime, "get_task_id"
                )
            except Exception:
                pass
        if not cluster_epoch or not run_id or not node_id or not actor_worker_id:
            raise ValueError("worker_runtime_identity_missing")
        process = WorkerProcessKey(
            cluster_epoch,
            node_id,
            actor_worker_id,
            os.getpid(),
            os.environ.get("UDFJIT_PROCESS_GENERATION", _PROCESS_GENERATION),
        )
        return cls(
            run_id,
            process,
            partition_id,
            task_attempt,
            refresh_partition_from_ray,
            tenant_namespace,
        )

    def event_attribution(self) -> tuple[str, str]:
        """Return the current task identity for a long-lived Ray Worker process."""

        partition_id = self.partition_id
        if self.refresh_partition_from_ray:
            try:
                import ray

                partition_id = (
                    _runtime_context_value(
                        ray.get_runtime_context(),
                        "get_task_id",
                    )
                    or partition_id
                )
            except Exception:
                pass
        return partition_id, self.task_attempt


@dataclass(frozen=True)
class RuntimeTarget:
    python_version: str
    cpython_cinderx_soabi: str
    cpu_features: tuple[str, ...]

    @classmethod
    def current(cls) -> "RuntimeTarget":
        features = tuple(
            sorted(
                {
                    value.strip()
                    for value in os.environ.get("UDFJIT_CPU_FEATURES", "").split(",")
                    if value.strip()
                }
            )
        )
        if not features:
            features = (platform.machine().lower(),)
        soabi = str(sysconfig.get_config_var("SOABI") or "")
        if not soabi:
            raise ValueError("runtime_soabi_missing")
        return cls(platform.python_version(), soabi, features)


@dataclass(frozen=True)
class WorkerGuardOverrides:
    """Controlled qualification seam; the production Wrapper never supplies it."""

    artifact_content_sha256: str | None = None
    experiment_manifest_sha256: str | None = None
    semantic_hash: str | None = None
    logical_schema: str | None = None
    callable_code_sha256: str | None = None
    target_python: str | None = None
    target_soabi: str | None = None
    cpu_features: tuple[str, ...] | None = None


def _user_function(original_callable: Callable[..., Any]) -> types.FunctionType:
    if type(original_callable) is not types.FunctionType:
        raise ValueError("unsupported_original_callable")
    wrapped = vars(original_callable).get("__wrapped__")
    if wrapped is None:
        return original_callable
    if type(wrapped) is not types.FunctionType or vars(wrapped).get("__wrapped__") is not None:
        raise ValueError("invalid_wrapped_callable_identity")
    return wrapped


def _fallback_identity(original_callable: Callable[..., Any]) -> FallbackIdentity:
    return capture(CaptureRequest(_user_function(original_callable))).fallback_identity


def _extract_scalar_value(
    original_callable: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> float:
    if kwargs:
        raise OuterGuardError(OuterGuardRejectCode.INPUT_SHAPE_MISMATCH)
    wrapped = (
        vars(original_callable).get("__wrapped__")
        if type(original_callable) is types.FunctionType
        else None
    )
    expected = 2 if wrapped is not None else 1
    if len(args) != expected:
        raise OuterGuardError(OuterGuardRejectCode.INPUT_SHAPE_MISMATCH)
    value = args[-1]
    if type(value) is not float:
        raise OuterGuardError(OuterGuardRejectCode.INPUT_TYPE_MISMATCH)
    return value


class WorkerScalarAdapter:
    """Lazy Daft Worker adapter implementing the U5 whole-UDF safety boundary."""

    def __init__(
        self,
        *,
        candidate_id: str,
        original_callable: Callable[..., Any],
        carrier: ProductionCarrierState,
        logical_schema: str,
        context: WorkerRuntimeContext,
        target_provider: Callable[[], RuntimeTarget] = RuntimeTarget.current,
        provider_factory: ScalarProviderFactory | None = None,
        event_sink: RuntimeEventSink = DEFAULT_RUNTIME_REPORT,
        artifact_loader: ArtifactLoader = _PROCESS_ARTIFACT_LOADER,
    ) -> None:
        if not candidate_id or not callable(original_callable):
            raise ValueError("invalid_worker_candidate")
        if not carrier.finalized or not logical_schema:
            raise ValueError("worker_candidate_not_finalized")
        if context.process.pid != os.getpid():
            raise ValueError("worker_context_process_mismatch")
        self.candidate_id = candidate_id
        self.original_callable = original_callable
        self.carrier = carrier
        self.logical_schema = logical_schema
        self.context = context
        self._target_provider = target_provider
        self._provider_factory = provider_factory or CinderXScalarProviderFactory()
        self._event_sink = event_sink
        self._artifact_loader = artifact_loader
        self._loader_namespace = LoaderNamespace(
            context.run_id,
            context.tenant_namespace,
            context.process.process_generation,
        )
        self._cache: ProcessVariantCache[ScalarProviderVariant] = ProcessVariantCache(
            context.process
        )
        self._physicalizer = ScalarPhysicalizer(
            epoch=context.process.cluster_epoch,
            process=ProcessIdentity(
                context.process.pid,
                context.process.process_generation,
            ),
        )
        self._artifact: PortableUdfArtifact | None = None
        self._expectation: OuterGuardExpectation | None = None
        self._key: VariantKey | None = None

    @property
    def owner_pid(self) -> int:
        return self.context.process.pid

    def _emit(
        self,
        stage: str,
        decision: str,
        reason_code: str,
        *,
        key: VariantKey | None = None,
        artifact_hash: str = "",
        code_hash: str = "",
        execution_mode: str = "",
        attribution: tuple[str, str] | None = None,
    ) -> None:
        try:
            partition_id, task_attempt = (
                attribution
                if attribution is not None
                else self.context.event_attribution()
            )
            self._event_sink.emit(
                RuntimeEvent(
                    stage,
                    decision,
                    reason_code,
                    self.context.run_id,
                    self.context.process,
                    "" if key is None else key.sha256,
                    artifact_hash,
                    code_hash,
                    partition_id,
                    task_attempt,
                    execution_mode,
                )
            )
        except Exception:
            pass

    def _load_and_reverify(
        self,
        *,
        attribution: tuple[str, str],
    ) -> tuple[PortableUdfArtifact, VariantKey]:
        if self._artifact is not None and self._key is not None:
            return self._artifact, self._key
        payload_hash = self.carrier.handle.content_sha256
        try:
            artifact = self._artifact_loader.load(
                self.carrier.handle,
                self._loader_namespace,
            )
        except ArtifactLoadError as error:
            if error.code in {
                ArtifactLoadRejectCode.HANDLE_INVALID,
                ArtifactLoadRejectCode.CONTENT_MISMATCH,
            }:
                raise OuterGuardError(
                    OuterGuardRejectCode.ARTIFACT_MISMATCH
                ) from error
            raise
        if not secrets.compare_digest(artifact.content_sha256, payload_hash):
            raise OuterGuardError(OuterGuardRejectCode.ARTIFACT_MISMATCH)
        verify_semantic_module(
            artifact.semantic_core_module,
            max_nodes=artifact.manifest.max_nodes,
            max_constants=artifact.manifest.max_constants,
        )
        verify_semantic_region_graph(
            artifact.semantic_core_module,
            artifact.semantic_region_graph,
        )
        identity = _fallback_identity(self.original_callable)
        if identity != artifact.fallback_identity:
            raise OuterGuardError(OuterGuardRejectCode.CALLABLE_MISMATCH)
        target = self._target_provider()
        schema_hash = _sha256_text(self.logical_schema)
        artifact_hash = payload_hash
        if target.python_version != artifact.manifest.target_python:
            raise OuterGuardError(OuterGuardRejectCode.TARGET_PYTHON_MISMATCH)
        if not target.cpython_cinderx_soabi.startswith(artifact.manifest.target_soabi):
            raise OuterGuardError(OuterGuardRejectCode.TARGET_SOABI_MISMATCH)
        expectation = OuterGuardExpectation(
            artifact_hash,
            self.carrier.manifest_sha256,
            artifact.semantic_core_module.semantic_hash,
            schema_hash,
            identity.code_sha256,
            artifact.manifest.target_python,
            target.cpython_cinderx_soabi,
            target.cpu_features,
        )
        key = VariantKey(
            self.context.process,
            artifact_hash,
            artifact.semantic_core_module.semantic_hash,
            schema_hash,
            identity.code_sha256,
            artifact.manifest.sha256,
            self.carrier.manifest_sha256,
            artifact.manifest.adapter_abi,
            artifact.manifest.runtime_abi,
            SCALAR_SLOT_ABI_VERSION,
            target.cpython_cinderx_soabi,
            target.cpu_features,
        )
        self._artifact = artifact
        self._expectation = expectation
        self._key = key
        self._emit(
            "artifact",
            "semantic_reverify",
            "verified",
            key=key,
            artifact_hash=artifact_hash,
            attribution=attribution,
        )
        return artifact, key

    def _observe(
        self, artifact: PortableUdfArtifact, overrides: WorkerGuardOverrides | None
    ) -> OuterGuardObservation:
        override = overrides or WorkerGuardOverrides()
        target = self._target_provider()
        identity = _fallback_identity(self.original_callable)
        return OuterGuardObservation(
            override.artifact_content_sha256 or self.carrier.handle.content_sha256,
            override.experiment_manifest_sha256 or self.carrier.manifest_sha256,
            override.semantic_hash
            or artifact.semantic_core_module.semantic_hash,
            _sha256_text(override.logical_schema or self.logical_schema),
            override.callable_code_sha256 or identity.code_sha256,
            override.target_python or target.python_version,
            override.target_soabi or target.cpython_cinderx_soabi,
            override.cpu_features or target.cpu_features,
            ProcessIdentity(
                self.context.process.pid, self.context.process.process_generation
            ),
        )

    def _fallback(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        reason_code: str,
        *,
        key: VariantKey | None = None,
        attribution: tuple[str, str],
    ) -> Any:
        self._emit(
            "execute",
            "fallback",
            reason_code,
            key=key,
            attribution=attribution,
        )
        return self.original_callable(*args, **kwargs)

    def invoke(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        guard_overrides: WorkerGuardOverrides | None = None,
    ) -> Any:
        attribution = self.context.event_attribution()
        try:
            value = _extract_scalar_value(self.original_callable, args, kwargs)
            artifact, key = self._load_and_reverify(
                attribution=attribution,
            )
            if self._expectation is None:
                raise RuntimeError("outer_guard_expectation_missing")
            guard_outer_entry(
                self._expectation,
                self._observe(artifact, guard_overrides),
                expected_process=ProcessIdentity(
                    self.context.process.pid, self.context.process.process_generation
                ),
            )
            resolution = self._cache.resolve(
                key, lambda: self._provider_factory.compile(artifact, key)
            )
            if resolution.decision is CacheDecision.MISMATCH or resolution.value is None:
                raise OuterGuardError(OuterGuardRejectCode.VARIANT_MISMATCH)
            variant = resolution.value
            if resolution.decision is CacheDecision.COMPILE:
                is_production_jit = (
                    isinstance(self._provider_factory, CinderXScalarProviderFactory)
                    and variant.execution_mode == "cinderx-jit"
                    and variant.intrinsic_load_count == 1
                    and variant.intrinsic_store_count >= 1
                )
                self._emit(
                    "jit" if is_production_jit else "provider",
                    "compile",
                    (
                        "cinderx_force_compile_verified"
                        if is_production_jit
                        else "non_jit_test_or_interpreter_compile"
                    ),
                    key=key,
                    artifact_hash=self.carrier.handle.content_sha256,
                    code_hash=variant.code_hash,
                    execution_mode=variant.execution_mode,
                    attribution=attribution,
                )
            else:
                is_production_jit = (
                    isinstance(self._provider_factory, CinderXScalarProviderFactory)
                    and variant.execution_mode == "cinderx-jit"
                    and variant.intrinsic_load_count == 1
                    and variant.intrinsic_store_count >= 1
                )
                self._emit(
                    "jit" if is_production_jit else "provider",
                    "hit",
                    (
                        "process_variant_cache"
                        if is_production_jit
                        else "non_jit_process_variant_cache"
                    ),
                    key=key,
                    artifact_hash=self.carrier.handle.content_sha256,
                    code_hash=variant.code_hash,
                    execution_mode=variant.execution_mode,
                    attribution=attribution,
                )
        except OuterGuardError as error:
            return self._fallback(
                args,
                kwargs,
                error.code.value,
                key=self._key,
                attribution=attribution,
            )
        except ArtifactLoadError as error:
            return self._fallback(
                args,
                kwargs,
                (
                    f"artifact_load_rejected:{error.code.value}"
                    + (
                        ""
                        if not error.detail
                        else f":{error.detail}"
                    )
                ),
                key=self._key,
                attribution=attribution,
            )
        except Exception as error:
            return self._fallback(
                args,
                kwargs,
                f"pre_semantics_failure:{type(error).__name__}",
                key=self._key,
                attribution=attribution,
            )

        boundary = CommitBoundary()
        try:
            frame = self._physicalizer.open_call(
                artifact.input_access_specs[0],
                artifact.output_access_spec,
                value,
                keepalive=args,
            )
            with frame:
                physical_value = frame.load_input()
                result = variant.execute(
                    physical_value,
                    boundary=boundary,
                )
                frame.stage_output(result)
                result = frame.publish_output()
        except PreSemanticsExecutionError as error:
            return self._fallback(
                args,
                kwargs,
                error.reason_code,
                key=key,
                attribution=attribution,
            )
        except Exception as error:
            if not boundary.committed:
                return self._fallback(
                    args,
                    kwargs,
                    (
                        "physicalization_failed:"
                        f"{type(error).__name__}"
                    ),
                    key=key,
                    attribution=attribution,
                )
            self._emit(
                "execute",
                "post_entry_failure",
                type(error).__name__,
                key=key,
                artifact_hash=self.carrier.handle.content_sha256,
                code_hash=variant.code_hash,
                execution_mode=variant.execution_mode,
                attribution=attribution,
            )
            raise
        self._emit(
            "execute",
            "semantic_execute",
            "success",
            key=key,
            artifact_hash=self.carrier.handle.content_sha256,
            code_hash=variant.code_hash,
            execution_mode=variant.execution_mode,
            attribution=attribution,
        )
        return result

    def close(self) -> None:
        self._cache.clear(lambda variant: variant.close())
        self._physicalizer.close()


def build_default_worker_adapter(wrapper: Any) -> WorkerScalarAdapter:
    return WorkerScalarAdapter(
        candidate_id=wrapper.candidate_id,
        original_callable=wrapper.original_callable,
        carrier=wrapper.carrier,
        logical_schema=wrapper.logical_schema,
        context=WorkerRuntimeContext.from_environment(),
    )
