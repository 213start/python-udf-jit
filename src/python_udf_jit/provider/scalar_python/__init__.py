"""One-slot float64 provider shared by the interpreter and CinderX seam."""

from python_udf_jit.provider.scalar_python.capability import (
    CapabilityHandle,
    CapabilityRegistry,
)
from python_udf_jit.provider.scalar_python.compiler import (
    CompiledScalarFunction,
    compile_scalar_region,
)
from python_udf_jit.provider.scalar_python.executor import (
    CinderXScalarProviderFactory,
    PreSemanticsExecutionError,
    ScalarExecutor,
    ScalarProviderVariant,
)

__all__ = [
    "CapabilityHandle",
    "CapabilityRegistry",
    "CompiledScalarFunction",
    "CinderXScalarProviderFactory",
    "PreSemanticsExecutionError",
    "ScalarExecutor",
    "ScalarProviderVariant",
    "compile_scalar_region",
]
