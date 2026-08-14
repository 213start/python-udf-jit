from __future__ import annotations

import importlib
import inspect
import os
import types
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from python_udf_jit.runtime.layout import ProcessIdentity


_ABSENT = object()


@dataclass(frozen=True)
class _WatcherSnapshot:
    kind: str
    owner: object
    key: object
    expected: object

    @classmethod
    def capture(cls, watcher: Any) -> "_WatcherSnapshot":
        kind = watcher.kind
        owner = watcher.owner
        key = watcher.key
        if kind == "dict_item":
            if type(owner) is not dict:
                raise TypeError("native_batch_watcher_owner_invalid")
            expected = owner[key] if key in owner else _ABSENT
        elif kind == "function_code":
            if type(owner) is not types.FunctionType or key != "__code__":
                raise TypeError("native_batch_function_watcher_invalid")
            expected = owner.__code__
        elif kind == "type_attr":
            if not isinstance(owner, type) or type(key) is not str:
                raise TypeError("native_batch_type_watcher_invalid")
            expected = inspect.getattr_static(owner, key)
        elif kind == "call_result_identity":
            if not callable(owner):
                raise TypeError("native_batch_call_watcher_invalid")
            expected = owner(key)
        else:
            raise TypeError("native_batch_watcher_kind_unsupported")
        return cls(kind, owner, key, expected)

    def matches(self) -> bool:
        try:
            if self.kind == "dict_item":
                current = (
                    self.owner[self.key]  # type: ignore[index]
                    if self.key in self.owner  # type: ignore[operator]
                    else _ABSENT
                )
            elif self.kind == "function_code":
                current = self.owner.__code__  # type: ignore[union-attr]
            elif self.kind == "type_attr":
                current = inspect.getattr_static(self.owner, self.key)
            elif self.kind == "call_result_identity":
                current = self.owner(self.key)  # type: ignore[operator]
            else:
                return False
        except (AttributeError, KeyError, TypeError):
            return False
        # Identity is conservative for every admitted watcher. Replacing an
        # immutable dependency with an equal object may reduce coverage, but it
        # can never authorize stale native code.
        return current is self.expected


@dataclass(frozen=True)
class _CallableSnapshot:
    function: types.FunctionType
    code: types.CodeType
    defaults: tuple[object, ...]
    keyword_defaults: tuple[tuple[str, object], ...]
    closure_values: tuple[object, ...]

    @classmethod
    def capture(cls, function: types.FunctionType) -> "_CallableSnapshot":
        closure = function.__closure__ or ()
        return cls(
            function,
            function.__code__,
            tuple(function.__defaults__ or ()),
            tuple(sorted((function.__kwdefaults__ or {}).items())),
            tuple(cell.cell_contents for cell in closure),
        )

    def matches(self) -> bool:
        function = self.function
        if function.__code__ is not self.code:
            return False
        defaults = tuple(function.__defaults__ or ())
        keyword_defaults = tuple(sorted((function.__kwdefaults__ or {}).items()))
        closure = function.__closure__ or ()
        try:
            closure_values = tuple(cell.cell_contents for cell in closure)
        except ValueError:
            return False
        return (
            len(defaults) == len(self.defaults)
            and all(left is right for left, right in zip(defaults, self.defaults))
            and len(keyword_defaults) == len(self.keyword_defaults)
            and all(
                left_name == right_name and left_value is right_value
                for (left_name, left_value), (right_name, right_value) in zip(
                    keyword_defaults,
                    self.keyword_defaults,
                )
            )
            and len(closure_values) == len(self.closure_values)
            and all(
                left is right
                for left, right in zip(closure_values, self.closure_values)
            )
        )


def _make_batch_loop(
    target: Callable[..., object],
    bound_arguments: Mapping[str, object],
) -> Callable[[list[object]], list[object]]:
    frozen_bound = dict(bound_arguments)
    if not frozen_bound:
        def native_batch_loop(values: list[object]) -> list[object]:
            outputs: list[object] = []
            append = outputs.append
            for value in values:
                append(target(value))
            return outputs
    else:
        def native_batch_loop(values: list[object]) -> list[object]:
            outputs: list[object] = []
            append = outputs.append
            for value in values:
                append(target(value, **frozen_bound))
            return outputs
    return native_batch_loop


class NativeBatchExecutor:
    """Process-local CinderX loop over one proven exact-Unicode column."""

    def __init__(
        self,
        *,
        process: ProcessIdentity,
        target_snapshot: _CallableSnapshot,
        wrapper_guard: Any,
        runtime_guard: Any | None,
        watcher_snapshots: tuple[_WatcherSnapshot, ...],
        batch_loop: Callable[[list[object]], list[object]],
        dictionary_capacity: int | None,
    ) -> None:
        self.owner_pid = process.pid
        self.process_generation = process.generation
        self._target_snapshot = target_snapshot
        self._wrapper_guard = wrapper_guard
        self._runtime_guard = runtime_guard
        self._watcher_snapshots = watcher_snapshots
        self._batch_loop = batch_loop
        self.dictionary_capacity = dictionary_capacity

    def __getstate__(self) -> object:
        raise TypeError("native batch executor is process-local")

    def matches_process(self, process: ProcessIdentity) -> bool:
        return (
            os.getpid() == self.owner_pid
            and process.pid == self.owner_pid
            and process.generation == self.process_generation
        )

    def guards_match(self, process: ProcessIdentity) -> bool:
        if not self.matches_process(process):
            return False
        try:
            if not self._wrapper_guard.matches():
                return False
            if not self._target_snapshot.matches():
                return False
            if (
                self._runtime_guard is not None
                and not self._runtime_guard.matches()
            ):
                return False
            return all(watcher.matches() for watcher in self._watcher_snapshots)
        except Exception:
            return False

    def invoke(self, columns: tuple[list[Any], ...]) -> list[Any]:
        if len(columns) != 1:
            raise ValueError("native_batch_input_arity_mismatch")
        # The list stays private until the caller validates and publishes the
        # Arrow output. A business exception escapes at its first lane.
        return self._batch_loop(columns[0])


def _force_compile(jit: Any, function: types.FunctionType) -> bool:
    try:
        compiled = jit.force_compile(function) is True
        return bool(compiled and jit.is_jit_compiled(function))
    except Exception:
        return False


def build_native_batch_executor(
    scalar_wrapper: Any,
    *,
    process: ProcessIdentity,
) -> NativeBatchExecutor | None:
    """Compile a guarded batch trampoline or fail before semantic entry."""

    try:
        from python_udf_jit.compiler.invariant_calls import (
            analyze_invariant_calls,
            analyze_value_cache,
        )
        from python_udf_jit.compiler.typed_frontend import (
            TypedCaptureError,
            capture_typed_loop,
        )
        from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            resolve_typed_loop_callable,
        )

        resolved = resolve_typed_loop_callable(scalar_wrapper.original_callable)
        if not resolved.wrapper_guard.matches():
            return None
        runtime_guard = None
        try:
            captured = capture_typed_loop(
                resolved.function,
                input_types=(EXACT_UNICODE,),
                bound_arguments=resolved.bound_arguments,
                allow_guarded_region=True,
            )
            runtime_guard = captured.runtime_guard
        except TypedCaptureError:
            captured = None
        value_plan = analyze_value_cache(resolved.function)
        # Typed capture is a semantic/guard proof, not an executable backend
        # proof.  Admitting it by itself previously sent otherwise valid Python
        # functions into unsupported CinderX HIR shapes and, after isolating the
        # call, added a costly compiled trampoline with no target acceleration.
        if value_plan is None:
            return None
        if value_plan is not None and value_plan.function is not resolved.function:
            return None
        invariant_plans = analyze_invariant_calls(
            resolved.function,
            bound_arguments=resolved.bound_arguments,
        )
        plans = (
            *invariant_plans,
            *((value_plan,) if value_plan is not None else ()),
        )
        watcher_snapshots = tuple(
            _WatcherSnapshot.capture(watcher)
            for plan in plans
            for watcher in plan.watchers
        )
        target_snapshot = _CallableSnapshot.capture(resolved.function)

        cinderx = importlib.import_module("cinderx")
        initializer = getattr(cinderx, "init", None)
        initialized = getattr(cinderx, "is_initialized", None)
        if callable(initializer) and (
            not callable(initialized) or not initialized()
        ):
            initializer()
        jit = importlib.import_module("cinderx.jit")

        # Attach every proven cache descriptor to the exact function it guards
        # before compiling any caller. CinderX remains authoritative for the
        # per-entry dynamic state watcher on value-cache hits.
        for plan in invariant_plans:
            plan.function.__udfjit_invariant_cache__ = plan.backend_descriptor()
            if not _force_compile(jit, plan.function):
                return None
        if value_plan is not None:
            resolved.function.__udfjit_value_cache__ = value_plan.backend_descriptor()
        if not _force_compile(jit, resolved.function):
            return None

        batch_loop = _make_batch_loop(
            resolved.function,
            resolved.bound_arguments,
        )
        if not _force_compile(jit, batch_loop):
            return None
        executor = NativeBatchExecutor(
            process=process,
            target_snapshot=target_snapshot,
            wrapper_guard=resolved.wrapper_guard,
            runtime_guard=runtime_guard,
            watcher_snapshots=watcher_snapshots,
            batch_loop=batch_loop,
            dictionary_capacity=(
                value_plan.capacity
                if (
                    value_plan is not None
                    and value_plan.input_type == "exact_unicode"
                    and value_plan.result_type == "exact_unicode"
                    # A dynamic entry guard must be observed on every logical
                    # row. Collapsing duplicate lanes would weaken that temporal
                    # contract, so only unguarded value plans are eligible.
                    and value_plan.entry_guard is None
                )
                else None
            ),
        )
        return executor if executor.guards_match(process) else None
    except Exception:
        return None
