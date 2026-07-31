from __future__ import annotations

import builtins
import dis
import hashlib
import os
import platform
import secrets
import sys
import sysconfig
import threading
import time
import types
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Mapping

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
from python_udf_jit.diagnostics.config import (
    DiagnosticPolicySnapshot,
    DiagnosticProfile,
    DiagnosticRuntimeContext as DiagnosticBootstrapContext,
    OFF_DIAGNOSTIC_POLICY,
    resolve_diagnostic_policy,
)
from python_udf_jit.governance.policy import (
    DEFAULT_MAINLINE_POLICY,
    PolicySnapshot,
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
from python_udf_jit.runtime.process_governor import ProcessVariantGovernor
from python_udf_jit.runtime.variant import VariantKey, WorkerProcessKey
from python_udf_jit.runtime.variant_manager import (
    ResolveKind,
    VariantManager,
    VariantNamespace,
)


_PROCESS_GENERATION = secrets.token_hex(16)
_PROCESS_ARTIFACT_LOADER = ArtifactLoader()


@dataclass
class _ProcessManagerEntry:
    manager: VariantManager[ScalarProviderVariant]
    last_used_ns: int
    clients: int


_PROCESS_VARIANT_MANAGERS: dict[
    tuple[WorkerProcessKey, VariantNamespace, str],
    _ProcessManagerEntry,
] = {}
_PROCESS_VARIANT_GOVERNORS: dict[
    tuple[WorkerProcessKey, str],
    ProcessVariantGovernor,
] = {}
_PROCESS_VARIANT_MANAGERS_LOCK = threading.Lock()
_CONTINUATION_LIVE_KIND = {
    BOOL_SCALAR_TYPE: LiveValueKind.BOOL,
    INT32_SCALAR_TYPE: LiveValueKind.INT32,
    INT64_SCALAR_TYPE: LiveValueKind.INT64,
    FLOAT32_SCALAR_TYPE: LiveValueKind.FLOAT32,
    FLOAT64_SCALAR_TYPE: LiveValueKind.FLOAT64,
}
_STABLE_RUNTIME_REASON_CODES = frozenset(
    {
        "artifact_load_rejected",
        "artifact_mismatch",
        "callable_mismatch",
        "cinderx_force_compile_verified",
        "circuit_open",
        "compile_capacity_exhausted",
        "compile_inflight",
        "compile_pool_closed",
        "compile_submitted",
        "continuation_proof_incomplete",
        "cpu_feature_mismatch",
        "descriptor_epoch_mismatch",
        "input_shape_mismatch",
        "input_type_mismatch",
        "invalid_context",
        "late_descriptor_miss",
        "manifest_mismatch",
        "negative_cache",
        "non_jit_process_variant_cache",
        "non_jit_test_or_interpreter_compile",
        "physicalization_failed",
        "post_entry_failure",
        "pre_semantics_failure",
        "process_mismatch",
        "process_variant_cache",
        "schema_mismatch",
        "semantic_mismatch",
        "success",
        "target_python_mismatch",
        "target_soabi_mismatch",
        "variant_mismatch",
        "variant_unavailable",
        "verified",
        "worker_process_mismatch",
    }
)


def _stable_runtime_reason(reason_code: str) -> str:
    if reason_code in _STABLE_RUNTIME_REASON_CODES:
        return reason_code
    return "pre_semantics_failure"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _variant_code_size(variant: ScalarProviderVariant) -> int:
    compiled = getattr(variant, "compiled", None)
    size = getattr(compiled, "code_size", None)
    if size is None:
        size = getattr(variant, "code_size", None)
    if type(size) is not int or size <= 0:
        raise ValueError("variant_code_size_unavailable")
    return size


def _manager_from_policy(
    context: WorkerRuntimeContext,
    namespace: VariantNamespace,
    *,
    governor: ProcessVariantGovernor | None = None,
    governor_owner: str | None = None,
) -> VariantManager[ScalarProviderVariant]:
    budgets = context.policy.budgets
    return VariantManager[ScalarProviderVariant](
        process=context.process,
        namespace=namespace,
        max_variants=budgets["variant_limit"],
        max_code_bytes=budgets["code_bytes"],
        max_compile_workers=budgets["compile_concurrency"],
        max_pending_compiles=budgets["compile_pending"],
        compile_timeout_ns=budgets["compile_timeout_ms"] * 1_000_000,
        negative_ttl_ns=budgets["negative_ttl_ms"] * 1_000_000,
        max_negative_entries=budgets["negative_cache_entries"],
        circuit_failure_threshold=budgets[
            "circuit_failure_threshold"
        ],
        circuit_reset_ns=budgets["circuit_reset_ms"] * 1_000_000,
        code_size=_variant_code_size,
        closer=lambda variant: variant.close(),
        process_governor=governor,
        governor_owner=governor_owner,
    )


def _process_variant_manager(
    context: WorkerRuntimeContext,
) -> VariantManager[ScalarProviderVariant]:
    namespace = VariantNamespace(
        context.run_id,
        context.tenant_namespace,
    )
    policy_sha256 = context.policy.sha256
    identity = (context.process, namespace, policy_sha256)
    now = time.monotonic_ns()
    with _PROCESS_VARIANT_MANAGERS_LOCK:
        idle_ns = (
            context.policy.budgets["namespace_idle_ms"] * 1_000_000
        )
        expired = [
            (key, value)
            for key, value in _PROCESS_VARIANT_MANAGERS.items()
            if key[0] == context.process
            and key[2] == policy_sha256
            and not value.clients
            and now - value.last_used_ns >= idle_ns
            and value.manager.can_retire()
        ]
        for key, value in expired:
            _PROCESS_VARIANT_MANAGERS.pop(key)
            value.manager.close()

        entry = _PROCESS_VARIANT_MANAGERS.get(identity)
        if entry is not None:
            entry.clients += 1
            entry.last_used_ns = now
            return entry.manager

        conflicting = [
            (key, value)
            for key, value in _PROCESS_VARIANT_MANAGERS.items()
            if key[0] == context.process
            and key[1] == namespace
            and key[2] != policy_sha256
        ]
        for key, value in conflicting:
            if value.clients or not value.manager.can_retire():
                raise ValueError("worker_policy_drift")
            _PROCESS_VARIANT_MANAGERS.pop(key)
            value.manager.close()

        process_entries = [
            (key, value)
            for key, value in _PROCESS_VARIANT_MANAGERS.items()
            if key[0] == context.process and key[2] == policy_sha256
        ]
        namespace_limit = context.policy.budgets[
            "process_namespace_limit"
        ]
        if len(process_entries) >= namespace_limit:
            retireable = [
                (key, value)
                for key, value in process_entries
                if not value.clients and value.manager.can_retire()
            ]
            if not retireable:
                raise ValueError("process_namespace_capacity_exhausted")
            retire_key, retire_entry = min(
                retireable,
                key=lambda item: item[1].last_used_ns,
            )
            _PROCESS_VARIANT_MANAGERS.pop(retire_key)
            retire_entry.manager.close()

        active_governors = {
            (key[0], key[2]) for key in _PROCESS_VARIANT_MANAGERS
        }
        for stale_key in tuple(_PROCESS_VARIANT_GOVERNORS):
            if (
                stale_key[0] == context.process
                and stale_key not in active_governors
            ):
                _PROCESS_VARIANT_GOVERNORS.pop(stale_key)

        governor_key = (context.process, policy_sha256)
        governor = _PROCESS_VARIANT_GOVERNORS.get(governor_key)
        if governor is None:
            budgets = context.policy.budgets
            governor = ProcessVariantGovernor(
                max_namespaces=budgets["process_namespace_limit"],
                max_variants=budgets["process_variant_limit"],
                max_code_bytes=budgets["process_code_bytes"],
            )
            _PROCESS_VARIANT_GOVERNORS[governor_key] = governor
        owner = hashlib.sha256(
            (
                f"{namespace.job_id}\0{namespace.tenant_id}\0"
                f"{policy_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        manager = _manager_from_policy(
            context,
            namespace,
            governor=governor,
            governor_owner=owner,
        )
        _PROCESS_VARIANT_MANAGERS[identity] = _ProcessManagerEntry(
            manager,
            now,
            1,
        )
        return manager


def _release_process_variant_manager(context: WorkerRuntimeContext) -> None:
    namespace = VariantNamespace(
        context.run_id,
        context.tenant_namespace,
    )
    identity = (context.process, namespace, context.policy.sha256)
    with _PROCESS_VARIANT_MANAGERS_LOCK:
        entry = _PROCESS_VARIANT_MANAGERS.get(identity)
        if entry is None or entry.clients <= 0:
            return
        entry.clients -= 1
        entry.last_used_ns = time.monotonic_ns()


def drain_process_compilation() -> None:
    """Wait only at an explicit qualification/diagnostic safe point."""

    with _PROCESS_VARIANT_MANAGERS_LOCK:
        managers = tuple(
            entry.manager for entry in _PROCESS_VARIANT_MANAGERS.values()
        )
    for manager in managers:
        manager.drain()


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
    policy: PolicySnapshot = DEFAULT_MAINLINE_POLICY
    diagnostic_policy: DiagnosticPolicySnapshot = OFF_DIAGNOSTIC_POLICY
    diagnostic_bootstrapped: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        policy: PolicySnapshot = DEFAULT_MAINLINE_POLICY,
        diagnostic_environment: Mapping[str, str] | None = None,
        diagnostic_runtime: DiagnosticBootstrapContext | None = None,
    ) -> "WorkerRuntimeContext":
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
        diagnostic_source = (
            os.environ
            if diagnostic_environment is None
            else diagnostic_environment
        )
        diagnostic_policy = resolve_diagnostic_policy(
            diagnostic_source,
            diagnostic_runtime or DiagnosticBootstrapContext(),
        )
        diagnostic_bootstrapped = False
        if diagnostic_policy.profile is DiagnosticProfile.FULL:
            if diagnostic_source.get("PYTHONJITUDFDIAGNOSTICS") != "1":
                raise ValueError(
                    "diagnostic_backend_bootstrap_missing"
                )
            if any(
                name in {"cinderx", "_cinderx", "cinderjit"}
                or name.startswith("cinderx.")
                for name in sys.modules
            ):
                raise ValueError(
                    "diagnostic_backend_already_initialized"
                )
            diagnostic_bootstrapped = True
        return cls(
            run_id,
            process,
            partition_id,
            task_attempt,
            refresh_partition_from_ray,
            tenant_namespace,
            policy,
            diagnostic_policy,
            diagnostic_bootstrapped,
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
        if context.policy.sha256 != carrier.policy.sha256:
            raise ValueError("worker_policy_hash_mismatch")
        if (
            context.diagnostic_policy.sha256
            != carrier.diagnostic_policy_sha256
        ):
            raise ValueError(
                "worker_diagnostic_policy_hash_mismatch"
            )
        if (
            context.diagnostic_policy.profile is DiagnosticProfile.FULL
            and not context.diagnostic_bootstrapped
        ):
            raise ValueError(
                "worker_diagnostic_backend_not_bootstrapped"
            )
        self.candidate_id = candidate_id
        self.original_callable = original_callable
        self.carrier = carrier
        self.logical_schema = logical_schema
        self.context = context
        self._target_provider = target_provider
        self._diagnostic_runtime = None
        if context.diagnostic_policy.enabled:
            from python_udf_jit.diagnostics.worker_runtime import (
                open_worker_diagnostic_runtime,
            )

            process_key = (
                "worker-" + _sha256_text(repr(context.process))[:24]
            )
            self._diagnostic_runtime = open_worker_diagnostic_runtime(
                context.diagnostic_policy,
                run_id=context.run_id,
                runtime_mode=context.policy.mode_ceiling,
                process_key=process_key,
                candidate_id=candidate_id,
                artifact_sha256=carrier.handle.content_sha256,
                user_function=_user_function(original_callable),
            )
        if provider_factory is None:
            self._provider_factory = (
                CinderXScalarProviderFactory()
                if self._diagnostic_runtime is None
                else CinderXScalarProviderFactory(
                    diagnostic_observer=self._diagnostic_runtime,
                )
            )
        else:
            self._provider_factory = provider_factory
        self._event_sink = event_sink
        self._artifact_loader = artifact_loader
        self._loader_namespace = LoaderNamespace(
            context.run_id,
            context.tenant_namespace,
            context.process.process_generation,
        )
        self._owns_variant_manager = not isinstance(
            self._provider_factory,
            CinderXScalarProviderFactory,
        )
        self._closed = False
        self._release_finalizer: weakref.finalize | None = None
        if self._owns_variant_manager:
            self._variants = _manager_from_policy(
                context,
                VariantNamespace(
                    context.run_id,
                    context.tenant_namespace,
                ),
            )
        else:
            self._variants = _process_variant_manager(context)
            self._release_finalizer = weakref.finalize(
                self,
                _release_process_variant_manager,
                context,
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

    def drain_compilation(self) -> None:
        """Wait at a diagnostic/test safe point, never from the UDF hot path."""

        self._variants.drain()

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
                    _stable_runtime_reason(reason_code),
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
            self.context.policy.version,
            self.context.policy.sha256,
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

    def _execute_variant(
        self,
        *,
        artifact: PortableUdfArtifact,
        key: VariantKey,
        variant: Any,
        value: float,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        continuation: InterpreterContinuation[object] | None,
        attribution: tuple[str, str],
    ) -> Any:
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
        except Exception:
            if not boundary.committed:
                return self._fallback(
                    args,
                    kwargs,
                    "physicalization_failed",
                    key=key,
                    attribution=attribution,
                )
            self._emit(
                "execute",
                "post_entry_failure",
                "post_entry_failure",
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
                    compiled_variant = self._provider_factory.compile(
                        artifact,
                        key,
                    )
                else:
                    compiled_variant = self._provider_factory.compile(
                        artifact,
                        key,
                        continuation=continuation,
                    )
                production_jit = (
                    isinstance(
                        self._provider_factory,
                        CinderXScalarProviderFactory,
                    )
                    and compiled_variant.execution_mode == "cinderx-jit"
                    and compiled_variant.intrinsic_load_count == 1
                    and (
                        compiled_variant.intrinsic_store_count >= 1
                        or compiled_variant.continuation_enabled
                    )
                )
                self._emit(
                    "jit" if production_jit else "provider",
                    "compile",
                    (
                        "cinderx_force_compile_verified"
                        if production_jit
                        else "non_jit_test_or_interpreter_compile"
                    ),
                    key=key,
                    artifact_hash=self.carrier.handle.content_sha256,
                    code_hash=compiled_variant.code_hash,
                    execution_mode=compiled_variant.execution_mode,
                    attribution=attribution,
                )
                return compiled_variant

            resolution = self._variants.resolve(
                key,
                compile_variant,
            )
            if resolution.kind is not ResolveKind.HIT:
                return self._fallback(
                    args,
                    kwargs,
                    resolution.reason_code,
                    key=key,
                    attribution=attribution,
                )
            if resolution.variant is None:
                raise OuterGuardError(OuterGuardRejectCode.VARIANT_MISMATCH)
        except OuterGuardError as error:
            return self._fallback(
                args,
                kwargs,
                error.code.value,
                key=self._key,
                attribution=attribution,
            )
        except ArtifactLoadError:
            return self._fallback(
                args,
                kwargs,
                "artifact_load_rejected",
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
        except Exception:
            return self._fallback(
                args,
                kwargs,
                "pre_semantics_failure",
                key=self._key,
                attribution=attribution,
            )

        lease = self._variants.acquire(key)
        try:
            variant = lease.__enter__()
        except KeyError:
            return self._fallback(
                args,
                kwargs,
                "variant_unavailable",
                key=key,
                attribution=attribution,
            )
        try:
            return self._execute_variant(
                artifact=artifact,
                key=key,
                variant=variant,
                value=value,
                args=args,
                kwargs=kwargs,
                continuation=continuation,
                attribution=attribution,
            )
        finally:
            lease.__exit__(None, None, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_variant_manager:
            self._variants.close()
        elif self._release_finalizer is not None:
            self._release_finalizer()
        self._physicalizer.close()
        if self._diagnostic_runtime is not None:
            self._diagnostic_runtime.finalize()


def build_default_worker_adapter(wrapper: Any) -> WorkerScalarAdapter:
    return WorkerScalarAdapter(
        candidate_id=wrapper.candidate_id,
        original_callable=wrapper.original_callable,
        carrier=wrapper.carrier,
        logical_schema=wrapper.logical_schema,
        context=WorkerRuntimeContext.from_environment(
            policy=wrapper.carrier.policy,
        ),
    )
