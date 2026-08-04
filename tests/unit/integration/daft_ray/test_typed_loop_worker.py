from __future__ import annotations

import functools
import unittest

from python_udf_jit.integration.daft_ray.typed_loop_worker import (
    WorkerTypedLoopAdapter,
    resolve_typed_loop_callable,
)
from python_udf_jit.provider.scalar_python.typed_loop import BackendCompilation


def _ratio_leaf(text: str, *, minimum: float) -> bool:
    if not text:
        return False
    accepted = sum(1 for character in text if character.isalnum())
    return accepted / len(text) >= minimum


def _make_filter(minimum: float):
    def predicate(text: str) -> bool:
        return _ratio_leaf(text, minimum=minimum)

    def set_minimum(value: float) -> None:
        nonlocal minimum
        minimum = value

    return predicate, set_minimum


def _make_framework_wrapper(function):
    def udf(text: str) -> bool:
        return function(text)

    return udf


def _make_receiver_trampoline(function):
    wrapped = _make_framework_wrapper(function)

    @functools.wraps(wrapped)
    def method(_self, *args, **kwargs):
        return wrapped(*args, **kwargs)

    return method


class _Backend:
    adapter_version = "typed-loop-worker-test-v1"

    def __init__(self) -> None:
        self.calls = 0

    def compile(self, _lowering) -> BackendCompilation:
        self.calls += 1
        return BackendCompilation(True, "test_typed_loop")


class WorkerTypedLoopAdapterTests(unittest.TestCase):
    def test_recursive_wrapper_resolution_binds_portable_constants(self) -> None:
        predicate, _ = _make_filter(0.5)
        wrapped = _make_framework_wrapper(predicate)

        resolved = resolve_typed_loop_callable(wrapped)

        self.assertIs(resolved.function, _ratio_leaf)
        self.assertEqual(resolved.bound_arguments, {"minimum": 0.5})
        self.assertEqual(resolved.wrapper_depth, 2)
        self.assertTrue(resolved.wrapper_guard.matches())

    def test_worker_compiles_once_after_threshold_and_reuses_variant(self) -> None:
        predicate, _ = _make_filter(0.5)
        backend = _Backend()
        adapter = WorkerTypedLoopAdapter(
            _make_framework_wrapper(predicate),
            candidate_id="candidate-test",
            call_threshold=2,
            backend=backend,
        )

        deferred = adapter.invoke(("abc-",), {})
        first_hit = adapter.invoke(("abc-",), {})
        second_hit = adapter.invoke(("---",), {})

        self.assertFalse(deferred.handled)
        self.assertEqual(deferred.reason_code, "runtime_call_threshold")
        self.assertTrue(first_hit.handled)
        self.assertTrue(first_hit.value)
        self.assertTrue(second_hit.handled)
        self.assertFalse(second_hit.value)
        self.assertEqual(backend.calls, 1)
        stats = adapter.snapshot()
        self.assertEqual(stats.compile_attempts, 1)
        self.assertEqual(stats.compile_successes, 1)
        self.assertEqual(stats.hits, 2)
        self.assertEqual(stats.wrapper_depth, 2)

    def test_framework_receiver_trampoline_is_not_a_typed_input(self) -> None:
        predicate, _ = _make_filter(0.5)
        adapter = WorkerTypedLoopAdapter(
            _make_receiver_trampoline(predicate),
            candidate_id="candidate-test",
            call_threshold=1,
            backend=_Backend(),
        )

        outcome = adapter.invoke((object(), "abc-"), {})

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.value)

    def test_empty_input_side_exits_to_the_original_callable(self) -> None:
        predicate, _ = _make_filter(0.5)
        adapter = WorkerTypedLoopAdapter(
            _make_framework_wrapper(predicate),
            candidate_id="candidate-test",
            call_threshold=1,
            backend=_Backend(),
        )

        outcome = adapter.invoke(("",), {})

        self.assertFalse(outcome.handled)
        self.assertEqual(outcome.reason_code, "typed_entry_guard_miss")
        self.assertFalse(predicate(""))

    def test_changed_wrapper_binding_invalidates_the_compiled_path(self) -> None:
        predicate, set_minimum = _make_filter(0.2)
        adapter = WorkerTypedLoopAdapter(
            _make_framework_wrapper(predicate),
            candidate_id="candidate-test",
            call_threshold=1,
            backend=_Backend(),
        )
        self.assertTrue(adapter.invoke(("abc-",), {}).handled)

        set_minimum(0.9)
        invalidated = adapter.invoke(("abc-",), {})

        self.assertFalse(invalidated.handled)
        self.assertEqual(invalidated.reason_code, "thin_wrapper_guard_miss")
        self.assertFalse(predicate("abc-"))
        self.assertEqual(adapter.snapshot().guard_misses, 1)

    def test_terminal_capture_failure_is_exposed_to_the_wrapper(self) -> None:
        def unsupported(value: str) -> str:
            return value.upper()

        adapter = WorkerTypedLoopAdapter(
            unsupported,
            candidate_id="candidate-test",
            call_threshold=1,
            backend=_Backend(),
        )

        first = adapter.invoke(("abc",), {})
        second = adapter.invoke(("abc",), {})

        self.assertFalse(first.handled)
        self.assertTrue(first.terminal)
        self.assertFalse(second.handled)
        self.assertTrue(second.terminal)


if __name__ == "__main__":
    unittest.main()
