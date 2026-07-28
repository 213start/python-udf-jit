from __future__ import annotations

import builtins
import dis
import hashlib
import os
import platform
import secrets
import sysconfig
import types
from dataclasses import dataclass
from typing import Any, Callable

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.compiler.reference import (
    reference_resume_live_names,
    reference_resume_semantic,
)
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
from python_udf_jit.provider.scalar_python.compiler import (
    require_single_python_barrier,
)
from python_udf_jit.provider.scalar_python.executor import (
    CinderXScalarProviderFactory,
    PreSemanticsExecutionError,
    ScalarProviderFactory,
    ScalarProviderVariant,
)
from python_udf_jit.runtime.continuation import (
    CONTINUATION_ABI_VERSION,
    CommitBoundary,
    ContinuationContract,
    ContinuationError,
    ContinuationState,
    InterpreterContinuation,
    LiveValueKind,
    LiveValueSpec,
    ResumeSourceMap,
)
from python_udf_jit.runtime.guards import (
    OuterGuardError,
    OuterGuardExpectation,
    OuterGuardObservation,
    OuterGuardRejectCode,
    guard_outer_entry,
)
from python_udf_jit.runtime.layout import (
    BOOL_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
    ProcessIdentity,
    SCALAR_SLOT_ABI_VERSION,
)
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer
from python_udf_jit.runtime.variant import (
    CacheDecision,
    ProcessVariantCache,
    VariantKey,
    WorkerProcessKey,
)


_PROCESS_GENERATION = secrets.token_hex(16)
_PROCESS_ARTIFACT_LOADER = ArtifactLoader()
_CONTINUATION_LIVE_KIND = {
    BOOL_SCALAR_TYPE: LiveValueKind.BOOL,
    INT32_SCALAR_TYPE: LiveValueKind.INT32,
    INT64_SCALAR_TYPE: LiveValueKind.INT64,
    FLOAT32_SCALAR_TYPE: LiveValueKind.FLOAT32,
    FLOAT64_SCALAR_TYPE: LiveValueKind.FLOAT64,
}


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
    user_function = _user_function(original_callable)
    identities = capture_identities(user_function)
    return FallbackIdentity(
        user_function.__module__,
        user_function.__qualname__,
        identities.code.sha256,
    )


def _resume_source_map(
    source_code: types.CodeType,
    bytecode_offset: int,
) -> ResumeSourceMap:
    if (
        type(bytecode_offset) is not int
        or bytecode_offset < 0
        or bytecode_offset > len(source_code.co_code)
    ):
        raise ValueError("continuation_source_offset_invalid")
    instructions = tuple(dis.get_instructions(source_code))
    if not instructions:
        raise ValueError("continuation_source_position_missing")
    instruction = next(
        (
            candidate
            for candidate in instructions
            if candidate.offset >= bytecode_offset
        ),
        instructions[-1],
    )
    positions = instruction.positions
    line = positions.lineno or source_code.co_firstlineno
    end_line = positions.end_lineno or line
    return ResumeSourceMap(
        CONTINUATION_ABI_VERSION,
        bytecode_offset,
        line,
        positions.col_offset,
        end_line,
        positions.end_col_offset,
    )


def _build_interpreter_continuation(
    artifact: PortableUdfArtifact,
    original_callable: Callable[..., Any],
) -> InterpreterContinuation[object] | None:
    module = artifact.semantic_core_module
    if not module.python_regions:
        return None
    try:
        region = require_single_python_barrier(module)
        user_function = _user_function(original_callable)
        identities = capture_identities(user_function)
        program = analyze_function(
            user_function,
            identities=identities,
        )
        worker_semantic = compile_semantic(program)
        if (
            identities.code.sha256
            != artifact.fallback_identity.code_sha256
            or identities.source.code_sha256 != module.function_id
            or worker_semantic.reason_code
            != "verified_scalar_graph_break"
            or worker_semantic.core_module != module
            or worker_semantic.region_graph
            != artifact.semantic_region_graph
            or region.source_end is None
        ):
            raise ValueError("continuation_source_identity_mismatch")
        source_regions = program.analysis.python_regions
        if len(source_regions) != 1:
            raise ValueError("continuation_source_region_mismatch")
        source_region = source_regions[0]
        if (
            source_region.start_offset != region.source_start
            or source_region.end_offset != region.source_end
            or source_region.resume_id != region.resume_id
            or len(source_region.live_in) != 1
        ):
            raise ValueError("continuation_source_region_mismatch")
        region_instructions = tuple(
            instruction
            for instruction in program.frontend.decoded_bytecode.instructions
            if instruction.offset in source_region.instruction_offsets
        )
        allowed_region_opcodes = {
            "LOAD_GLOBAL",
            "LOAD_FAST",
            "LOAD_FAST_BORROW",
            "LOAD_CONST",
            "CALL",
        }
        if (
            not region_instructions
            or region_instructions[0].opcode_name != "LOAD_GLOBAL"
            or region_instructions[-1].opcode_name != "CALL"
            or any(
                instruction.opcode_name not in allowed_region_opcodes
                for instruction in region_instructions
            )
            or sum(
                instruction.opcode_name == "LOAD_GLOBAL"
                for instruction in region_instructions
            )
            != 1
            or sum(
                instruction.opcode_name == "CALL"
                for instruction in region_instructions
            )
            != 1
        ):
            raise ValueError("continuation_region_opcode_mismatch")
        first_state = next(
            (
                state
                for state in (
                    program.frontend.control_flow_graph.instruction_states
                )
                if state.bytecode_offset == source_region.start_offset
            ),
            None,
        )
        if first_state is None or first_state.stack_before:
            raise ValueError("continuation_region_stack_mismatch")
        live_fast_indexes = {
            index
            for index, name in enumerate(first_state.locals_before)
            if name == source_region.live_in[0]
        }
        if not live_fast_indexes or any(
            instruction.argument not in live_fast_indexes
            for instruction in region_instructions
            if instruction.opcode_name
            in {"LOAD_FAST", "LOAD_FAST_BORROW"}
        ):
            raise ValueError("continuation_region_live_in_mismatch")
        constants = user_function.__code__.co_consts
        if any(
            instruction.argument is None
            or not 0 <= instruction.argument < len(constants)
            or type(constants[instruction.argument]) is not float
            for instruction in region_instructions
            if instruction.opcode_name == "LOAD_CONST"
        ):
            raise ValueError("continuation_region_constant_mismatch")
        resume_id = f"v1:{region.resume_id}"
        if (
            reference_resume_live_names(module, resume_id)
            != region.live_in
        ):
            raise ValueError("continuation_suffix_live_values_mismatch")
        input_spec = artifact.input_access_specs[0]
        kind = _CONTINUATION_LIVE_KIND.get(input_spec.scalar_type)
        live_definition = next(
            (
                operation
                for operation in module.operations
                if operation.result_id == region.live_in[0]
            ),
            None,
        )
        if (
            kind is not LiveValueKind.FLOAT64
            or live_definition is None
            or live_definition.result_type.value != FLOAT64_SCALAR_TYPE
            or live_definition.nullability.value != "non_null"
        ):
            raise ValueError("continuation_input_kind_unsupported")
        live_values = (
            LiveValueSpec(
                region.live_in[0],
                kind,
                nullable=False,
            ),
        )
        source_map = _resume_source_map(
            user_function.__code__,
            region.source_end,
        )

        def execute_python_region(value: object) -> object:
            if type(value) is not float:
                raise TypeError("continuation_region_value_not_float64")
            stack: list[object] = []
            namespace = user_function.__globals__
            for instruction in region_instructions:
                opname = instruction.opcode_name
                argument = instruction.argument
                if opname == "LOAD_GLOBAL":
                    if argument is None:
                        raise RuntimeError("continuation_global_index_missing")
                    name_index = argument >> 1
                    names = user_function.__code__.co_names
                    if not 0 <= name_index < len(names):
                        raise RuntimeError(
                            "continuation_global_index_invalid"
                        )
                    name = names[name_index]
                    if name in namespace:
                        resolved = namespace[name]
                    else:
                        builtins_value = namespace.get(
                            "__builtins__",
                            builtins,
                        )
                        if type(builtins_value) is dict:
                            builtins_namespace = builtins_value
                        elif type(builtins_value) is types.ModuleType:
                            builtins_namespace = vars(builtins_value)
                        else:
                            raise TypeError(
                                "continuation_builtins_namespace_invalid"
                            )
                        if name not in builtins_namespace:
                            raise NameError(name)
                        resolved = builtins_namespace[name]
                    stack.append(resolved)
                elif opname in {"LOAD_FAST", "LOAD_FAST_BORROW"}:
                    stack.append(value)
                elif opname == "LOAD_CONST":
                    if argument is None:
                        raise RuntimeError(
                            "continuation_constant_index_missing"
                        )
                    constant = user_function.__code__.co_consts[argument]
                    if type(constant) is not float:
                        raise TypeError(
                            "continuation_region_constant_not_float64"
                        )
                    stack.append(constant)
                elif opname == "CALL":
                    if argument is None or argument < 0:
                        raise RuntimeError(
                            "continuation_call_arity_invalid"
                        )
                    if len(stack) < argument + 1:
                        raise RuntimeError(
                            "continuation_call_stack_underflow"
                        )
                    call_arguments = (
                        tuple(stack[-argument:])
                        if argument
                        else ()
                    )
                    if argument:
                        del stack[-argument:]
                    target = stack.pop()
                    if not callable(target):
                        raise TypeError(
                            "continuation_global_not_callable"
                        )
                    stack.append(target(*call_arguments))
            if len(stack) != 1:
                raise RuntimeError("continuation_region_stack_invalid")
            return value

        def resume_semantic_suffix(state: ContinuationState) -> object:
            region_result = execute_python_region(
                state.values[region.live_in[0]]
            )
            return reference_resume_semantic(
                module,
                resume_id,
                {region.live_in[0]: region_result},
            )

        contract = ContinuationContract(
            abi_version=CONTINUATION_ABI_VERSION,
            resume_id=resume_id,
            source_identity=identities.source,
            source_code=user_function.__code__,
            resume_code=resume_semantic_suffix.__code__,
            source_map=source_map,
            live_values=live_values,
            proof_complete=True,
        )
        return InterpreterContinuation(contract, resume_semantic_suffix)
    except (ContinuationError, TypeError, ValueError) as error:
        raise PreSemanticsExecutionError(
            "continuation_proof_incomplete"
        ) from error


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
        self._continuation_initialized = False
        self._continuation: InterpreterContinuation[object] | None = None

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
            if not self._continuation_initialized:
                self._continuation = _build_interpreter_continuation(
                    artifact,
                    self.original_callable,
                )
                self._continuation_initialized = True
            continuation = self._continuation

            def compile_variant() -> ScalarProviderVariant:
                if continuation is None:
                    return self._provider_factory.compile(
                        artifact,
                        key,
                    )
                return self._provider_factory.compile(
                    artifact,
                    key,
                    continuation=continuation,
                )

            resolution = self._cache.resolve(
                key,
                compile_variant,
            )
            if resolution.decision is CacheDecision.MISMATCH or resolution.value is None:
                raise OuterGuardError(OuterGuardRejectCode.VARIANT_MISMATCH)
            variant = resolution.value
            if resolution.decision is CacheDecision.COMPILE:
                is_production_jit = (
                    isinstance(self._provider_factory, CinderXScalarProviderFactory)
                    and variant.execution_mode == "cinderx-jit"
                    and variant.intrinsic_load_count == 1
                    and (
                        variant.intrinsic_store_count >= 1
                        or getattr(
                            variant,
                            "continuation_enabled",
                            False,
                        )
                    )
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
                    and (
                        variant.intrinsic_store_count >= 1
                        or getattr(
                            variant,
                            "continuation_enabled",
                            False,
                        )
                    )
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
        except PreSemanticsExecutionError as error:
            return self._fallback(
                args,
                kwargs,
                error.reason_code,
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
                if continuation is None:
                    result = variant.execute(
                        physical_value,
                        boundary=boundary,
                    )
                else:
                    result = variant.execute(
                        physical_value,
                        boundary=boundary,
                        continuation=continuation,
                    )
                frame.stage_output(result)
                result = frame.publish_output()
        except PreSemanticsExecutionError as error:
            if not boundary.committed:
                return self._fallback(
                    args,
                    kwargs,
                    error.reason_code,
                    key=key,
                    attribution=attribution,
                )
            self._emit(
                "execute",
                "post_entry_failure",
                error.reason_code,
                key=key,
                artifact_hash=self.carrier.handle.content_sha256,
                code_hash=variant.code_hash,
                execution_mode=variant.execution_mode,
                attribution=attribution,
            )
            raise
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
