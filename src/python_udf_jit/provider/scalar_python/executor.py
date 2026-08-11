from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import importlib
from typing import Callable, Protocol

from python_udf_jit.protocol.artifact import PortableUdfArtifact
from python_udf_jit.provider.scalar_python.capability import (
    CapabilityHandle,
    CapabilityRegistry,
    PreparedCapabilityPair,
)
from python_udf_jit.provider.scalar_python.compiler import (
    CompiledScalarFunction,
    ScalarLoweringHooks,
    compile_semantic_scalar_region,
)
from python_udf_jit.runtime.layout import (
    BOOL_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
    CinderXScalarSlotBackend,
)
from python_udf_jit.runtime.continuation import (
    CommitBoundary,
    ContinuationError,
    InterpreterContinuation,
    SideExit,
    side_exit_from_cinderx_result,
)
from python_udf_jit.runtime.variant import VariantKey


_HELPER_SUFFIX = {
    BOOL_SCALAR_TYPE: "bool",
    INT32_SCALAR_TYPE: "i32",
    INT64_SCALAR_TYPE: "i64",
    FLOAT32_SCALAR_TYPE: "f32",
    FLOAT64_SCALAR_TYPE: "f64",
}
_HIR_TYPE_NAME = {
    BOOL_SCALAR_TYPE: "Bool",
    INT32_SCALAR_TYPE: "I32",
    INT64_SCALAR_TYPE: "I64",
    FLOAT32_SCALAR_TYPE: "F32",
    FLOAT64_SCALAR_TYPE: "F64",
}


class PreSemanticsExecutionError(RuntimeError):
    """Internal failure before the compiled function has been entered."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


_LOAD_SCALAR_OUTPUT = object()


def _execute_compiled_region(
    compiled: Callable[[object, object], object],
    arguments: tuple[object, object],
    boundary: CommitBoundary,
    continuation: InterpreterContinuation[object] | None,
) -> object:
    # This explicit transition is the conservative semantic entry. After it,
    # no failure may replay the whole UDF.
    boundary.commit()
    region_result = compiled(*arguments)
    if continuation is not None:
        side_exit = side_exit_from_cinderx_result(
            region_result,
            contract=continuation.contract,
            boundary=boundary,
        )
        if side_exit is not None:
            region_result = side_exit
    if isinstance(region_result, SideExit):
        if region_result.boundary is not boundary:
            raise ContinuationError("side_exit_boundary_mismatch")
        if continuation is None:
            raise ContinuationError("interpreter_continuation_missing")
        return continuation.resume(region_result)
    return _LOAD_SCALAR_OUTPUT


class ScalarExecutor:
    """Synchronous two-slot borrow/write/execute/publish scope."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._prepared_backend_pair: PreparedCapabilityPair | None = None

    def _validate_compiled_registry(
        self,
        compiled: Callable[[object, object], object],
    ) -> None:
        compiled_registry_id = getattr(
            compiled,
            "registry_id",
            self._registry.registry_id,
        )
        if (
            compiled_registry_id is not None
            and compiled_registry_id != self._registry.registry_id
        ):
            raise ValueError(
                "compiled scalar function belongs to another registry"
            )

    def prepare_backend_pair(
        self,
        compiled: Callable[[object, object], object],
        input_handle: CapabilityHandle,
        output_handle: CapabilityHandle,
    ) -> PreparedCapabilityPair:
        self._validate_compiled_registry(compiled)
        if getattr(compiled, "argument_kind", "capability_pair") != "backend_pair":
            raise ValueError("compiled scalar function does not use backend arguments")
        prepared = self._prepared_backend_pair
        if prepared is None:
            prepared = self._registry.prepare_pair(
                input_handle,
                output_handle,
            )
            self._prepared_backend_pair = prepared
        elif prepared.handles != (input_handle, output_handle):
            raise ValueError("scalar executor is already bound to another slot pair")
        return prepared

    def _arguments(
        self,
        compiled: Callable[[object, object], object],
        input_handle: CapabilityHandle,
        output_handle: CapabilityHandle,
    ) -> tuple[object, object]:
        argument_kind = getattr(
            compiled,
            "argument_kind",
            "capability_pair",
        )
        if argument_kind == "capability_pair":
            return input_handle, output_handle
        raise ValueError("unknown compiled scalar argument kind")

    def execute(
        self,
        compiled: Callable[[object, object], object],
        input_handle: CapabilityHandle,
        output_handle: CapabilityHandle,
        value: object,
    ) -> object:
        self._validate_compiled_registry(compiled)
        if getattr(compiled, "argument_kind", "capability_pair") == "backend_pair":
            prepared = self.prepare_backend_pair(
                compiled,
                input_handle,
                output_handle,
            )
            with prepared:
                prepared.write_input(value)
                compiled(*prepared.execution_handles)
                return prepared.load_output()
        with self._registry.borrow(input_handle) as input_borrowed:
            with self._registry.borrow(output_handle) as output_borrowed:
                input_borrowed.write_scalar(value)
                arguments = self._arguments(
                    compiled,
                    input_handle,
                    output_handle,
                )
                compiled(*arguments)
                guarded_output = self._registry.guard_data_handle(
                    output_handle
                )
                return self._registry.data_load_scalar(guarded_output)

    def execute_guarded(
        self,
        compiled: Callable[[object, object], object],
        input_handle: CapabilityHandle,
        output_handle: CapabilityHandle,
        value: object,
        *,
        boundary: CommitBoundary,
        continuation: InterpreterContinuation[object] | None = None,
    ) -> object:
        """Execute with an explicit no-replay commit at function entry."""

        if not isinstance(boundary, CommitBoundary):
            raise TypeError("commit boundary required")
        boundary.require_pre_commit()
        try:
            self._validate_compiled_registry(compiled)
            argument_kind = getattr(
                compiled,
                "argument_kind",
                "capability_pair",
            )
            if argument_kind == "backend_pair":
                prepared = self.prepare_backend_pair(
                    compiled,
                    input_handle,
                    output_handle,
                )
                with prepared:
                    prepared.write_input(value)
                    arguments = prepared.execution_handles
                    execution_result = _execute_compiled_region(
                        compiled,
                        arguments,
                        boundary,
                        continuation,
                    )
                    if execution_result is not _LOAD_SCALAR_OUTPUT:
                        return execution_result
                    result = prepared.load_output()
            else:
                with self._registry.borrow(input_handle) as input_borrowed:
                    with self._registry.borrow(output_handle) as output_borrowed:
                        input_borrowed.write_scalar(value)
                        arguments = self._arguments(
                            compiled,
                            input_handle,
                            output_handle,
                        )
                        execution_result = _execute_compiled_region(
                            compiled,
                            arguments,
                            boundary=boundary,
                            continuation=continuation,
                        )
                        if execution_result is not _LOAD_SCALAR_OUTPUT:
                            return execution_result
                        guarded_output = self._registry.guard_data_handle(
                            output_handle
                        )
                        result = self._registry.data_load_scalar(
                            guarded_output
                        )
        except PreSemanticsExecutionError:
            raise
        except BaseException as error:
            if not boundary.committed:
                raise PreSemanticsExecutionError(
                    f"slot_setup_failed:{type(error).__name__}"
                ) from error
            raise
        return result


@dataclass
class ScalarProviderVariant:
    key: VariantKey
    compiled: CompiledScalarFunction
    registry: CapabilityRegistry
    input_handle: CapabilityHandle
    output_handle: CapabilityHandle
    executor: ScalarExecutor
    intrinsic_counts: tuple[tuple[str, int], ...]
    continuation_enabled: bool = False

    def __post_init__(self) -> None:
        if getattr(self.compiled, "argument_kind", "capability_pair") == "backend_pair":
            self.executor.prepare_backend_pair(
                self.compiled,
                self.input_handle,
                self.output_handle,
            )
        else:
            self.preflight_descriptor()

    @property
    def code_hash(self) -> str:
        return self.compiled.code_hash

    @property
    def execution_mode(self) -> str:
        return self.compiled.execution_mode

    @property
    def intrinsic_load_count(self) -> int:
        return sum(
            count
            for name, count in self.intrinsic_counts
            if name.startswith("LoadUdfData")
        )

    @property
    def intrinsic_store_count(self) -> int:
        return sum(
            count
            for name, count in self.intrinsic_counts
            if name.startswith("StoreUdfData")
        )

    def preflight_descriptor(self) -> None:
        # The registry checks both descriptors before native code sees either
        # process-local Capsule. CinderX still performs a dominating native
        # guard before each data-aware load and store.
        self.registry.descriptor(self.input_handle)
        self.registry.descriptor(self.output_handle)

    def execute(
        self,
        value: object,
        *,
        boundary: CommitBoundary,
        continuation: InterpreterContinuation[object] | None = None,
    ) -> object:
        return self.executor.execute_guarded(
            self.compiled,
            self.input_handle,
            self.output_handle,
            value,
            boundary=boundary,
            continuation=continuation,
        )

    def close(self) -> None:
        self.registry.release(self.output_handle)
        self.registry.release(self.input_handle)


class ScalarProviderFactory(Protocol):
    def compile(
        self,
        artifact: PortableUdfArtifact,
        key: VariantKey,
        *,
        continuation: InterpreterContinuation[object] | None = None,
    ) -> ScalarProviderVariant: ...


class CinderXDiagnosticObserver(Protocol):
    def span(self, stage: str, identity: str = ""): ...

    def provenance_sink(
        self,
        artifact: PortableUdfArtifact,
        key: VariantKey,
    ): ...

    def prepare_compilation(
        self,
        compiled: CompiledScalarFunction,
        key: VariantKey,
    ) -> str: ...

    def record_compilation(
        self,
        jit_module: object,
        compiled: CompiledScalarFunction,
        key: VariantKey,
        compile_instance_id: str,
    ) -> None: ...


_NOOP_DIAGNOSTIC_SPAN = nullcontext()


class CinderXScalarProviderFactory:
    """Production factory: exact helpers, force-compile, and HIR proof."""

    def __init__(
        self,
        *,
        jit_module_name: str = "cinderx.jit",
        runtime_module_name: str = "cinderjit",
        diagnostic_observer: CinderXDiagnosticObserver | None = None,
    ) -> None:
        self._jit_module_name = jit_module_name
        self._runtime_module_name = runtime_module_name
        self._diagnostic_observer = diagnostic_observer

    def _diagnostic_span(self, stage: str, identity: str):
        if self._diagnostic_observer is None:
            return _NOOP_DIAGNOSTIC_SPAN
        try:
            return self._diagnostic_observer.span(stage, identity)
        except Exception:
            return _NOOP_DIAGNOSTIC_SPAN

    def _provenance_sink(
        self,
        artifact: PortableUdfArtifact,
        key: VariantKey,
    ):
        if self._diagnostic_observer is None:
            return None
        try:
            return self._diagnostic_observer.provenance_sink(artifact, key)
        except Exception:
            return None

    def _prepare_diagnostics(
        self,
        compiled: CompiledScalarFunction,
        key: VariantKey,
    ) -> str:
        if self._diagnostic_observer is None:
            return ""
        try:
            return self._diagnostic_observer.prepare_compilation(
                compiled,
                key,
            )
        except Exception:
            return ""

    def _record_diagnostics(
        self,
        jit: object,
        compiled: CompiledScalarFunction,
        key: VariantKey,
        compile_instance_id: str,
    ) -> None:
        if self._diagnostic_observer is None or not compile_instance_id:
            return
        try:
            self._diagnostic_observer.record_compilation(
                jit,
                compiled,
                key,
                compile_instance_id,
            )
        except Exception:
            pass

    def compile(
        self,
        artifact: PortableUdfArtifact,
        key: VariantKey,
        *,
        continuation: InterpreterContinuation[object] | None = None,
    ) -> ScalarProviderVariant:
        if len(artifact.input_access_specs) != 1:
            raise RuntimeError("scalar_provider_requires_one_input")
        input_spec = artifact.input_access_specs[0]
        output_spec = artifact.output_access_spec
        input_suffix = _HELPER_SUFFIX.get(input_spec.scalar_type)
        output_suffix = _HELPER_SUFFIX.get(output_spec.scalar_type)
        if input_suffix is None or output_suffix is None:
            raise RuntimeError("scalar_provider_type_unsupported")

        jit = importlib.import_module(self._jit_module_name)
        runtime = importlib.import_module(self._runtime_module_name)
        if not bool(jit.is_enabled()):
            raise RuntimeError("cinderx_jit_disabled")
        hooks = ScalarLoweringHooks(
            runtime._udf_guard_data_handle,
            runtime._udf_data_is_null,
            getattr(runtime, f"_udf_data_load_{input_suffix}"),
            getattr(runtime, f"_udf_data_store_{output_suffix}"),
            runtime._udf_data_store_null,
            runtime._udf_build_continuation_payload,
        )

        registry = CapabilityRegistry(epoch=key.process.cluster_epoch)
        input_handle = registry.register(
            CinderXScalarSlotBackend(
                scalar_type=input_spec.scalar_type,
                nullable=input_spec.nullable,
                module_name=self._runtime_module_name,
            )
        )
        try:
            output_handle = registry.register(
                CinderXScalarSlotBackend(
                    scalar_type=output_spec.scalar_type,
                    nullable=output_spec.nullable,
                    module_name=self._runtime_module_name,
                )
            )
        except BaseException:
            registry.release(input_handle)
            raise
        try:
            with self._diagnostic_span(
                "scalar_lowering",
                getattr(key, "sha256", ""),
            ):
                compiled = compile_semantic_scalar_region(
                    artifact.semantic_core_module,
                    artifact.semantic_region_graph,
                    input_spec=input_spec,
                    output_spec=output_spec,
                    hooks=hooks,
                    execution_mode="cinderx-jit",
                    argument_kind="backend_pair",
                    continuation_contract=(
                        None
                        if continuation is None
                        else continuation.contract
                    ),
                    provenance_sink=self._provenance_sink(artifact, key),
                )
            compile_instance_id = self._prepare_diagnostics(
                compiled,
                key,
            )
            if continuation is not None and not bool(
                runtime._udf_register_continuation_code(
                    compiled.jit_function
                )
            ):
                raise RuntimeError(
                    "cinderx_continuation_registration_rejected"
                )
            with self._diagnostic_span(
                "cinderx_compile",
                compile_instance_id,
            ):
                if not bool(jit.force_compile(compiled.jit_function)):
                    raise RuntimeError("cinderx_force_compile_rejected")
                if not bool(jit.is_jit_compiled(compiled.jit_function)):
                    raise RuntimeError("cinderx_compile_not_observed")
                opcode_counts = jit.get_function_hir_opcode_counts(
                    compiled.jit_function
                )
            input_hir = _HIR_TYPE_NAME[input_spec.scalar_type]
            output_hir = _HIR_TYPE_NAME[output_spec.scalar_type]
            required = {
                f"LoadUdfData{input_hir}": 1,
            }
            if continuation is None:
                required[f"StoreUdfData{output_hir}"] = 1
            if input_spec.nullable:
                required["IsUdfDataNull"] = 1
            observed = tuple(
                sorted(
                    (
                        name,
                        int(opcode_counts.get(name, 0)),
                    )
                    for name in required
                )
            )
            if any(
                dict(observed)[name] != expected
                for name, expected in required.items()
            ):
                raise RuntimeError(
                    "cinderx_data_intrinsic_not_observed"
                )
            self._record_diagnostics(
                jit,
                compiled,
                key,
                compile_instance_id,
            )
            return ScalarProviderVariant(
                key,
                compiled,
                registry,
                input_handle,
                output_handle,
                ScalarExecutor(registry),
                observed,
                continuation is not None,
            )
        except BaseException:
            registry.release(output_handle)
            registry.release(input_handle)
            raise
