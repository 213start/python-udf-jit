from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Callable, Protocol

from python_udf_jit.protocol.artifact import PortableUdfArtifact
from python_udf_jit.provider.scalar_python.capability import CapabilityHandle, CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import (
    CompiledScalarFunction,
    compile_scalar_region,
)
from python_udf_jit.runtime.layout import CinderXScalarSlotBackend
from python_udf_jit.runtime.variant import VariantKey


class PreSemanticsExecutionError(RuntimeError):
    """Internal failure before the compiled function has been entered."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ScalarExecutor:
    """Synchronous borrow/write/execute scope for one process-local slot."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def execute(
        self,
        compiled: Callable[[object], float],
        handle: CapabilityHandle,
        value: float,
    ) -> float:
        if type(value) is not float:
            raise TypeError("scalar executor accepts exactly one Python float")
        compiled_registry_id = getattr(compiled, "registry_id", self._registry.registry_id)
        if compiled_registry_id is not None and compiled_registry_id != self._registry.registry_id:
            raise ValueError("compiled scalar function belongs to another registry")
        with self._registry.borrow(handle) as borrowed:
            borrowed.write_f64(value)
            argument_kind = getattr(compiled, "argument_kind", "capability")
            if argument_kind == "capability":
                argument: object = handle
            elif argument_kind == "backend":
                argument = borrowed.execution_handle
            else:
                raise ValueError("unknown compiled scalar argument kind")
            result = compiled(argument)  # type: ignore[arg-type]
            if type(result) is not float:
                raise TypeError("compiled scalar function must return a Python float")
            return result

    def execute_guarded(
        self,
        compiled: Callable[[object], float],
        handle: CapabilityHandle,
        value: float,
    ) -> float:
        """Execute with an explicit no-replay commit at compiled-function entry."""

        if type(value) is not float:
            raise PreSemanticsExecutionError("input_type_mismatch")
        try:
            compiled_registry_id = getattr(
                compiled, "registry_id", self._registry.registry_id
            )
            if (
                compiled_registry_id is not None
                and compiled_registry_id != self._registry.registry_id
            ):
                raise ValueError("compiled scalar function belongs to another registry")
            with self._registry.borrow(handle) as borrowed:
                borrowed.write_f64(value)
                argument_kind = getattr(compiled, "argument_kind", "capability")
                if argument_kind == "capability":
                    argument: object = handle
                elif argument_kind == "backend":
                    argument = borrowed.execution_handle
                else:
                    raise ValueError("unknown compiled scalar argument kind")
                # Commit point: after this call begins, no exception is eligible
                # for whole-UDF fallback or replay.
                result = compiled(argument)  # type: ignore[arg-type]
        except PreSemanticsExecutionError:
            raise
        except BaseException as error:
            # Errors from entering/using the compiled function must propagate.
            # Only setup failures can be converted to fail-open.
            if "result" not in locals() and "argument" not in locals():
                raise PreSemanticsExecutionError(
                    f"slot_setup_failed:{type(error).__name__}"
                ) from error
            raise
        if type(result) is not float:
            raise TypeError("compiled scalar function must return a Python float")
        return result


@dataclass
class ScalarProviderVariant:
    key: VariantKey
    compiled: CompiledScalarFunction
    registry: CapabilityRegistry
    handle: CapabilityHandle
    executor: ScalarExecutor
    intrinsic_load_count: int

    @property
    def code_hash(self) -> str:
        return self.compiled.code_hash

    @property
    def execution_mode(self) -> str:
        return self.compiled.execution_mode

    def preflight_descriptor(self) -> None:
        # CapabilityRegistry performs the full ABI/type/epoch/access/process and
        # generation/token check. The CinderX function still performs its own
        # dominating native guard before LOAD_DATA_F64.
        self.registry.descriptor(self.handle)

    def execute(self, value: float) -> float:
        self.preflight_descriptor()
        return self.executor.execute_guarded(self.compiled, self.handle, value)

    def close(self) -> None:
        self.registry.release(self.handle)


class ScalarProviderFactory(Protocol):
    def compile(
        self, artifact: PortableUdfArtifact, key: VariantKey
    ) -> ScalarProviderVariant: ...


class CinderXScalarProviderFactory:
    """Production factory: exact CinderX helpers, force-compile, and HIR proof."""

    def __init__(
        self,
        *,
        jit_module_name: str = "cinderx.jit",
        runtime_module_name: str = "cinderjit",
    ) -> None:
        self._jit_module_name = jit_module_name
        self._runtime_module_name = runtime_module_name

    def compile(
        self, artifact: PortableUdfArtifact, key: VariantKey
    ) -> ScalarProviderVariant:
        jit = importlib.import_module(self._jit_module_name)
        runtime = importlib.import_module(self._runtime_module_name)
        if not bool(jit.is_enabled()):
            raise RuntimeError("cinderx_jit_disabled")

        registry = CapabilityRegistry(epoch=key.process.cluster_epoch)
        handle = registry.register(
            CinderXScalarSlotBackend(module_name=self._runtime_module_name)
        )
        try:
            compiled = compile_scalar_region(
                artifact.core_module,
                artifact.region,
                guard_function=runtime._udf_guard_data_handle,
                load_function=runtime._udf_data_load_f64,
                execution_mode="cinderx-jit",
                argument_kind="backend",
            )
            if not bool(jit.force_compile(compiled.jit_function)):
                raise RuntimeError("cinderx_force_compile_rejected")
            if not bool(jit.is_jit_compiled(compiled.jit_function)):
                raise RuntimeError("cinderx_compile_not_observed")
            opcode_counts = jit.get_function_hir_opcode_counts(compiled.jit_function)
            intrinsic_count = int(opcode_counts.get("LoadUdfDataF64", 0))
            if intrinsic_count != 1:
                raise RuntimeError("cinderx_data_intrinsic_not_observed")
            return ScalarProviderVariant(
                key,
                compiled,
                registry,
                handle,
                ScalarExecutor(registry),
                intrinsic_count,
            )
        except BaseException:
            registry.release(handle)
            raise
