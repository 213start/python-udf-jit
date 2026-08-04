from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from python_udf_jit.compiler.invariant_calls import (
    InvariantCallPlan,
    ValueCachePlan,
)

INVARIANT_CACHE_EXECUTION_MODE = "cinderx_guarded_invariant_cache"
VALUE_CACHE_EXECUTION_MODE = "cinderx_guarded_value_cache"


@dataclass(frozen=True)
class InvariantBackendCompilation:
    jit_compiled: bool
    execution_mode: str
    reason_code: str
    hir_opcode_counts: tuple[tuple[str, int], ...] = ()


class InvariantCallBackend(Protocol):
    def compile(self, plan: InvariantCallPlan) -> InvariantBackendCompilation: ...


class ValueCacheBackend(Protocol):
    def compile(self, plan: ValueCachePlan) -> InvariantBackendCompilation: ...


class CinderXInvariantCallBackend:
    """Transfer a proof descriptor; CinderX owns the actual optimization."""

    def compile(self, plan: InvariantCallPlan) -> InvariantBackendCompilation:
        import cinderx

        initializer = getattr(cinderx, "init", None)
        initialized = getattr(cinderx, "is_initialized", None)
        if callable(initializer) and (
            not callable(initialized) or not initialized()
        ):
            initializer()
        import cinderx.jit

        plan.function.__udfjit_invariant_cache__ = plan.backend_descriptor()
        try:
            compiled = cinderx.jit.force_compile(plan.function) is True
            raw_counts = (
                cinderx.jit.get_function_hir_opcode_counts(plan.function) or {}
            )
        except Exception:
            return InvariantBackendCompilation(
                False,
                INVARIANT_CACHE_EXECUTION_MODE,
                "cinderx_invariant_cache_compile_failed",
            )
        counts = tuple(
            sorted((str(name), int(count)) for name, count in raw_counts.items())
        )
        count_map = dict(counts)
        capability_present = (
            count_map.get("CallStatic", 0) >= 2
            and count_map.get("CallStaticRetVoid", 0) >= 1
        )
        jit_compiled = bool(
            compiled
            and capability_present
            and cinderx.jit.is_jit_compiled(plan.function)
        )
        return InvariantBackendCompilation(
            jit_compiled,
            INVARIANT_CACHE_EXECUTION_MODE,
            (
                "cinderx_invariant_cache_compiled"
                if jit_compiled
                else "cinderx_invariant_cache_unavailable"
            ),
            counts,
        )


class CinderXValueCacheBackend:
    """Transfer exact-value facts; CinderX owns lookup and update control flow."""

    def compile(self, plan: ValueCachePlan) -> InvariantBackendCompilation:
        import cinderx

        initializer = getattr(cinderx, "init", None)
        initialized = getattr(cinderx, "is_initialized", None)
        if callable(initializer) and (
            not callable(initialized) or not initialized()
        ):
            initializer()
        import cinderx.jit

        plan.function.__udfjit_value_cache__ = plan.backend_descriptor()
        try:
            compiled = cinderx.jit.force_compile(plan.function) is True
            raw_counts = (
                cinderx.jit.get_function_hir_opcode_counts(plan.function) or {}
            )
        except Exception:
            return InvariantBackendCompilation(
                False,
                VALUE_CACHE_EXECUTION_MODE,
                "cinderx_value_cache_compile_failed",
            )
        counts = tuple(
            sorted((str(name), int(count)) for name, count in raw_counts.items())
        )
        count_map = dict(counts)
        capability_present = (
            count_map.get("CallStatic", 0) >= 2
            and count_map.get("CallStaticRetVoid", 0) >= 1
        )
        jit_compiled = bool(
            compiled
            and capability_present
            and cinderx.jit.is_jit_compiled(plan.function)
        )
        return InvariantBackendCompilation(
            jit_compiled,
            VALUE_CACHE_EXECUTION_MODE,
            (
                "cinderx_value_cache_compiled"
                if jit_compiled
                else "cinderx_value_cache_unavailable"
            ),
            counts,
        )
