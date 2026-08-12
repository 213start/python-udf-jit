from __future__ import annotations

import json
import functools
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.columnar import (
    ArrowBorrowScope,
    ColumnarBatchWrapper,
    _process_identity,
    columnar_boundary_proven,
    reset_columnar_counters_for_testing,
    snapshot_columnar_counters,
)
from python_udf_jit.integration.daft_ray.native_batch import (
    build_native_batch_executor,
)
from python_udf_jit.integration.daft_ray.invocation_layout import (
    InvocationLayoutContract,
)
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper
from python_udf_jit.runtime.guards import (
    DescriptorGuardError,
    DescriptorRejectCode,
    guard_arrow_batch_descriptor,
)
from python_udf_jit.runtime.layout import ArrowBatchDescriptor
from tests.unit.compiler.test_invariant_calls import (
    _choose_location,
    _render_frozen_record,
)


def nullable_receiver(_receiver: object, value: str | None) -> str:
    return "NULL-CALLED" if value is None else value.upper()


def fail_on_bad(_receiver: object, value: str) -> str:
    if value == "bad":
        raise ValueError("bad-row")
    return value


def remap_text(value: str) -> str:
    return value.translate(str.maketrans({"α": "a", "β": "b"}))


def effectful_text(value: str) -> str:
    print(value)
    return value


def replacement_remap_text(value: str) -> str:
    return value


def receiver_trampoline(function):
    @functools.wraps(function)
    def method(_receiver, *args, **kwargs):
        return function(*args, **kwargs)

    return method


class FakeArrowArray:
    def __init__(self, values, *, physical_type="large_string", offset=0):
        self.values = list(values)
        self.type = physical_type
        self.offset = offset
        self.null_count = sum(value is None for value in self.values)

    def __len__(self):
        return len(self.values)

    def buffers(self):
        return (object() if self.null_count else None, object(), object())

    def to_pylist(self):
        return list(self.values)


class FakeChunkedArray:
    def __init__(self, chunks):
        self.chunks = tuple(chunks)
        self.type = "large_string"
        self.offset = 0
        self.null_count = sum(chunk.null_count for chunk in self.chunks)

    def __len__(self):
        return sum(len(chunk) for chunk in self.chunks)

    def to_pylist(self):
        return [value for chunk in self.chunks for value in chunk.to_pylist()]


class FakeSeries:
    def __init__(self, array):
        self.array = array

    def to_arrow(self):
        return self.array


class GuardOrderedArrowArray(FakeArrowArray):
    def __init__(self, values, executor):
        super().__init__(values)
        self.executor = executor

    def to_pylist(self):
        if not self.executor.guard_checked:
            raise AssertionError("native guard must dominate Arrow data load")
        return super().to_pylist()


class FakePyArrow:
    @staticmethod
    def large_string():
        return "large_string"

    @staticmethod
    def array(values, *, type=None):
        return FakeArrowArray(values, physical_type=type or "large_string")


class FakeNativeExecutor:
    def __init__(self, function, *, process_matches=True, guards_match=True):
        self.function = function
        self.process_matches = process_matches
        self.guard_result = guards_match
        self.guard_checked = False
        self.invocations = 0

    def matches_process(self, _process):
        return self.process_matches

    def guards_match(self, _process):
        self.guard_checked = True
        return self.guard_result

    def invoke(self, columns):
        self.invocations += 1
        return [self.function(value) for value in columns[0]]


class FakeCinderXJit:
    def __init__(self, *, accept=True):
        self.accept = accept
        self.compiled = set()
        self.force_calls = []

    def force_compile(self, function):
        self.force_calls.append(function)
        if self.accept:
            self.compiled.add(id(function))
        return self.accept

    def is_jit_compiled(self, function):
        return id(function) in self.compiled


def fake_cinderx_imports(jit):
    cinderx = SimpleNamespace(init=lambda: None, is_initialized=lambda: True)
    return lambda name: cinderx if name == "cinderx" else jit


def make_batch_wrapper(function=nullable_receiver):
    scalar = FallbackOnlyWrapper(
        candidate_id="columnar-test",
        original_callable=function,
        carrier=ProductionCarrierState.placeholder("columnar-test", "a" * 64),
    )
    scalar.finalize(
        "{}",
        "projection",
        invocation_layout=InvocationLayoutContract.for_types(
            ("string",),
            "string",
            epoch="epoch-a",
        ),
    )
    return ColumnarBatchWrapper(scalar)


def make_two_input_batch_wrapper(function):
    scalar = FallbackOnlyWrapper(
        candidate_id="columnar-two-input-test",
        original_callable=function,
        carrier=ProductionCarrierState.placeholder(
            "columnar-two-input-test", "b" * 64
        ),
    )
    scalar.finalize(
        "{}",
        "projection",
        invocation_layout=InvocationLayoutContract.for_types(
            ("string", "string"),
            "string",
            epoch="epoch-a",
        ),
    )
    return ColumnarBatchWrapper(scalar)


class ColumnarBatchTest(unittest.TestCase):
    def setUp(self):
        reset_columnar_counters_for_testing()

    def test_boundary_admission_comes_from_typed_effect_proof(self):
        func = SimpleNamespace(return_dtype="string")
        self.assertTrue(
            columnar_boundary_proven(func, receiver_trampoline(remap_text))
        )
        self.assertFalse(
            columnar_boundary_proven(func, receiver_trampoline(effectful_text))
        )
        self.assertFalse(
            columnar_boundary_proven(
                SimpleNamespace(return_dtype="int64"),
                receiver_trampoline(remap_text),
            )
        )

    def test_large_string_descriptor_is_address_free_and_guards_every_load(self):
        array = FakeChunkedArray(
            (FakeArrowArray(["a", None], offset=2), FakeArrowArray(["b"]))
        )
        with ArrowBorrowScope(array, epoch="epoch-a") as scope:
            descriptor = scope.descriptor
            self.assertIsInstance(descriptor, ArrowBatchDescriptor)
            self.assertEqual(descriptor.physical_type, "large_string")
            self.assertEqual(descriptor.offset_width_bits, 64)
            self.assertEqual(descriptor.chunk_lengths, (2, 1))
            self.assertEqual(descriptor.chunk_offsets, (2, 0))
            self.assertEqual(descriptor.null_count, 1)
            self.assertEqual(scope.load_pylist(), ["a", None, "b"])
            document = descriptor.to_document()
            self.assertEqual(ArrowBatchDescriptor.from_document(document), descriptor)
            self.assertNotIn("address", repr(document).lower())
            with self.assertRaises(DescriptorGuardError) as raised:
                guard_arrow_batch_descriptor(
                    descriptor,
                    expected_epoch="other",
                    expected_borrow_id=descriptor.borrow_id,
                    expected_process=descriptor.process,
                )
            self.assertEqual(raised.exception.code, DescriptorRejectCode.EPOCH_MISMATCH)
        with self.assertRaises(DescriptorGuardError) as raised:
            scope.load_pylist()
        self.assertEqual(raised.exception.code, DescriptorRejectCode.BORROW_EXPIRED)

    def test_process_generation_guard_rejects_inherited_borrow(self):
        scope = ArrowBorrowScope(FakeArrowArray(["a"]), epoch="epoch-a")
        with scope:
            with mock.patch(
                "python_udf_jit.integration.daft_ray.columnar.os.getpid",
                return_value=os.getpid() + 1000,
            ):
                with self.assertRaises(DescriptorGuardError) as raised:
                    scope.load_pylist()
        self.assertEqual(raised.exception.code, DescriptorRejectCode.PROCESS_MISMATCH)

    def test_batch_preserves_receiver_null_order_and_output_type(self):
        wrapper = make_batch_wrapper()
        with (
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
            mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(["a", None, "b"])))

        self.assertEqual(result.to_pylist(), ["A", "NULL-CALLED", "B"])
        self.assertEqual(str(result.type), "large_string")
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["batches"], 1)
        self.assertEqual(counters["batch_boundary_hits"], 1)
        self.assertEqual(counters["rows"], 3)
        self.assertEqual(counters["arrow_borrows"], 1)
        self.assertEqual(counters["python_scalar_fallback_rows"], 3)
        self.assertEqual(counters["vector_batches"], 0)
        self.assertEqual(counters["vector_unavailable_batches"], 1)
        self.assertEqual(counters["native_jit_rows"], 0)
        self.assertEqual(counters["postcommit_replays"], 0)
        self.assertEqual(counters["native_batch_unavailable_batches"], 1)

    def test_native_batch_enters_once_without_per_lane_scalar_wrapper_calls(self):
        scalar_calls = []

        def scalar_receiver(_receiver, value):
            scalar_calls.append(value)
            return value

        wrapper = make_batch_wrapper(scalar_receiver)
        executor = FakeNativeExecutor(str.upper)
        wrapper._native_batch_executor = executor
        array = GuardOrderedArrowArray(["a", "b", "c"], executor)
        with mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}):
            result = wrapper(object(), FakeSeries(array))
        self.assertEqual(result.to_pylist(), ["A", "B", "C"])
        self.assertEqual(executor.invocations, 1)
        self.assertEqual(scalar_calls, [])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["native_batch_batches"], 1)
        self.assertEqual(counters["native_batch_rows"], 3)
        self.assertEqual(counters["native_batch_unavailable_batches"], 0)
        self.assertEqual(counters["native_jit_rows"], 0)
        self.assertEqual(counters["python_scalar_fallback_rows"], 0)
        self.assertEqual(counters["vector_batches"], 0)
        self.assertEqual(counters["vector_unavailable_batches"], 1)

    def test_native_compile_rejection_is_precommit_scalar_fallback(self):
        scalar_calls = []

        def scalar_receiver(_receiver, value):
            scalar_calls.append(value)
            return value.upper()

        wrapper = make_batch_wrapper(scalar_receiver)
        with (
            mock.patch(
                "python_udf_jit.integration.daft_ray.native_batch."
                "build_native_batch_executor",
                return_value=None,
            ) as build,
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
            mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(["a", "b"])))
        self.assertEqual(result.to_pylist(), ["A", "B"])
        self.assertEqual(scalar_calls, ["a", "b"])
        build.assert_called_once()
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["native_batch_batches"], 0)
        self.assertEqual(counters["native_batch_unavailable_batches"], 1)

    def test_native_executor_is_rebuilt_after_process_mismatch(self):
        wrapper = make_batch_wrapper()
        inherited = FakeNativeExecutor(str.lower, process_matches=False)
        rebuilt = FakeNativeExecutor(str.upper)
        wrapper._native_batch_executor = inherited
        with (
            mock.patch(
                "python_udf_jit.integration.daft_ray.native_batch."
                "build_native_batch_executor",
                return_value=rebuilt,
            ) as build,
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(["a"])))
        self.assertEqual(result.to_pylist(), ["A"])
        self.assertEqual(inherited.invocations, 0)
        self.assertEqual(rebuilt.invocations, 1)
        build.assert_called_once()

    def test_business_exception_in_native_batch_is_not_scalar_replayed(self):
        scalar_calls = []
        native_calls = []

        def scalar_receiver(_receiver, value):
            scalar_calls.append(value)
            return value

        def native_target(value):
            native_calls.append(value)
            if value == "bad":
                raise ValueError("native-bad-row")
            return value

        wrapper = make_batch_wrapper(scalar_receiver)
        executor = FakeNativeExecutor(native_target)
        wrapper._native_batch_executor = executor
        with mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}):
            with self.assertRaisesRegex(ValueError, "native-bad-row"):
                wrapper(
                    object(),
                    FakeSeries(FakeArrowArray(["ok", "bad", "later"])),
                )
        self.assertEqual(native_calls, ["ok", "bad"])
        self.assertEqual(scalar_calls, [])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["native_batch_batches"], 1)
        self.assertEqual(counters["native_batch_rows"], 3)
        self.assertEqual(counters["published_batches"], 0)
        self.assertEqual(counters["postcommit_replays"], 0)

    def test_builder_force_compiles_exact_target_and_batch_loop(self):
        wrapper = make_batch_wrapper(receiver_trampoline(remap_text))
        jit = FakeCinderXJit()
        with mock.patch(
            "python_udf_jit.integration.daft_ray.native_batch."
            "importlib.import_module",
            side_effect=fake_cinderx_imports(jit),
        ):
            executor = build_native_batch_executor(
                wrapper.scalar_wrapper,
                process=_process_identity(),
            )
        self.assertIsNotNone(executor)
        self.assertIn(remap_text, jit.force_calls)
        self.assertEqual(len(jit.force_calls), 2)
        self.assertEqual(executor.invoke((["α", "β"],)), ["a", "b"])
        with self.assertRaises(TypeError):
            pickle.dumps(executor)

    def test_builder_compile_rejection_has_no_executor(self):
        wrapper = make_batch_wrapper(receiver_trampoline(remap_text))
        jit = FakeCinderXJit(accept=False)
        with mock.patch(
            "python_udf_jit.integration.daft_ray.native_batch."
            "importlib.import_module",
            side_effect=fake_cinderx_imports(jit),
        ):
            executor = build_native_batch_executor(
                wrapper.scalar_wrapper,
                process=_process_identity(),
            )
        self.assertIsNone(executor)

    def test_value_and_invariant_descriptors_attach_before_exact_compile(self):
        wrapper = make_batch_wrapper(
            receiver_trampoline(_render_frozen_record)
        )
        jit = FakeCinderXJit()
        try:
            with mock.patch(
                "python_udf_jit.integration.daft_ray.native_batch."
                "importlib.import_module",
                side_effect=fake_cinderx_imports(jit),
            ):
                executor = build_native_batch_executor(
                    wrapper.scalar_wrapper,
                    process=_process_identity(),
                )
            self.assertIsNotNone(executor)
            self.assertIn(_render_frozen_record, jit.force_calls)
            self.assertIn(_choose_location, jit.force_calls)
            self.assertIn("__udfjit_value_cache__", vars(_render_frozen_record))
            self.assertIn("__udfjit_invariant_cache__", vars(_choose_location))
        finally:
            vars(_render_frozen_record).pop("__udfjit_value_cache__", None)
            vars(_choose_location).pop("__udfjit_invariant_cache__", None)

    def test_live_target_code_drift_falls_back_before_arrow_load(self):
        wrapper = make_batch_wrapper(receiver_trampoline(remap_text))
        jit = FakeCinderXJit()
        with mock.patch(
            "python_udf_jit.integration.daft_ray.native_batch."
            "importlib.import_module",
            side_effect=fake_cinderx_imports(jit),
        ):
            executor = build_native_batch_executor(
                wrapper.scalar_wrapper,
                process=_process_identity(),
            )
        self.assertIsNotNone(executor)

        class TrackingExecutor:
            guard_checked = False

            def matches_process(self, process):
                return executor.matches_process(process)

            def guards_match(self, process):
                self.guard_checked = True
                return executor.guards_match(process)

            def invoke(self, columns):
                return executor.invoke(columns)

        tracking = TrackingExecutor()
        wrapper._native_batch_executor = tracking
        original_code = remap_text.__code__
        remap_text.__code__ = replacement_remap_text.__code__
        try:
            array = GuardOrderedArrowArray(["α"], tracking)
            with (
                mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
                mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
            ):
                result = wrapper(object(), FakeSeries(array))
        finally:
            remap_text.__code__ = original_code
        self.assertEqual(result.to_pylist(), ["α"])
        self.assertEqual(snapshot_columnar_counters()["native_batch_batches"], 0)
        self.assertEqual(
            snapshot_columnar_counters()["native_batch_unavailable_batches"],
            1,
        )

    def test_precommit_layout_failure_falls_back_once_without_double_counting(self):
        calls = []

        def receiver(_receiver, value):
            calls.append(value)
            return value

        wrapper = make_batch_wrapper(receiver)
        series = FakeSeries(FakeArrowArray(["a", "b"], physical_type="opaque"))
        with (
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
            mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
        ):
            result = wrapper(object(), series)
        self.assertEqual(result.to_pylist(), ["a", "b"])
        self.assertEqual(calls, ["a", "b"])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["precommit_failures"], 1)
        self.assertEqual(counters["rows"], 2)
        self.assertEqual(counters["published_batches"], 1)
        self.assertEqual(counters["postcommit_replays"], 0)

    def test_mismatched_batch_lengths_never_enter_scalar_semantics(self):
        calls = []

        def receiver(_receiver, left, right):
            calls.append((left, right))
            return left + right

        wrapper = make_two_input_batch_wrapper(receiver)
        with (
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
            mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
        ):
            with self.assertRaisesRegex(
                ValueError, "columnar_fallback_input_length_mismatch"
            ):
                wrapper(
                    object(),
                    FakeSeries(FakeArrowArray(["a", "b"])),
                    FakeSeries(FakeArrowArray(["x"])),
                )
        self.assertEqual(calls, [])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["precommit_failures"], 1)
        self.assertEqual(counters["rows"], 0)
        self.assertEqual(counters["published_batches"], 0)

    def test_business_exception_keeps_first_exception_and_never_replays(self):
        calls = []

        def receiver(_receiver, value):
            calls.append(value)
            return fail_on_bad(_receiver, value)

        wrapper = make_batch_wrapper(receiver)
        with (
            mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
            mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
        ):
            with self.assertRaisesRegex(ValueError, "bad-row"):
                wrapper(object(), FakeSeries(FakeArrowArray(["ok", "bad", "later"])))
        self.assertEqual(calls, ["ok", "bad"])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["published_batches"], 0)
        self.assertEqual(counters["postcommit_replays"], 0)

    def test_wrapper_pickle_roundtrip_and_diagnostic_snapshot(self):
        wrapper = pickle.loads(pickle.dumps(make_batch_wrapper()))
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict(sys.modules, {"pyarrow": FakePyArrow}),
                mock.patch.dict(
                    os.environ,
                    {
                        "UDFJIT_MODE": "off",
                        "UDFJIT_COLUMNAR_DIAGNOSTIC_DIR": directory,
                    },
                ),
            ):
                result = wrapper(object(), FakeSeries(FakeArrowArray(["x"])))
            files = tuple(Path(directory).glob("columnar-*.json"))
            self.assertEqual(len(files), 1)
            document = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(result.to_pylist(), ["X"])
        self.assertEqual(document["counters"]["rows"], 1)
        self.assertEqual(document["counters"]["vector_batches"], 0)


if __name__ == "__main__":
    unittest.main()
