from __future__ import annotations

import json
import functools
import os
import pickle
import re
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
    _index_external_record,
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


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def split_strip_join(value: str) -> str:
    if not value or not value.strip():
        return value
    parts = _SENTENCE_RE.split(value)
    return "\n\n".join(part.strip() for part in parts if part.strip())


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

    def slice(self, offset, length=None):
        values = (
            self.values[offset:]
            if length is None
            else self.values[offset:offset + length]
        )
        return FakeArrowArray(
            values,
            physical_type=self.type,
            offset=self.offset + offset,
        )


class FakeDictionaryArray(FakeArrowArray):
    def __init__(self, dictionary, indices, *, offset=0):
        self.dictionary = FakeArrowArray(dictionary)
        self.indices = FakeArrowArray(indices, physical_type="int32")
        super().__init__(
            [self.dictionary.values[index] for index in self.indices.values],
            physical_type=(
                "dictionary<values=large_string, indices=int32, ordered=0>"
            ),
            offset=offset,
        )

    def combine_chunks(self):
        return self


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

    def combine_chunks(self):
        return FakeArrowArray(fake_arrow_values(self))


class FakeSeries:
    def __init__(self, array):
        self.array = array

    def to_arrow(self):
        return self.array


class GuardOrderedArrowArray(FakeArrowArray):
    def __init__(self, values, executor):
        super().__init__(values)
        self.executor = executor
        self.dictionary_encode_calls = 0

    def to_pylist(self):
        if not self.executor.guard_checked:
            raise AssertionError("native guard must dominate Arrow data load")
        return super().to_pylist()


def fake_arrow_values(array):
    if isinstance(array, FakeChunkedArray):
        return [value for chunk in array.chunks for value in chunk.values]
    return list(array.values)


class FakeArrowCompute:
    @staticmethod
    def dictionary_decode(array):
        return FakeArrowArray(fake_arrow_values(array))

    @staticmethod
    def dictionary_encode(array):
        executor = getattr(array, "executor", None)
        if executor is not None and not executor.guard_checked:
            raise AssertionError("native guard must dominate dictionary encode")
        if hasattr(array, "dictionary_encode_calls"):
            array.dictionary_encode_calls += 1
        dictionary = []
        positions = {}
        indices = []
        for value in fake_arrow_values(array):
            if value not in positions:
                positions[value] = len(dictionary)
                dictionary.append(value)
            indices.append(positions[value])
        return FakeDictionaryArray(dictionary, indices)

    @staticmethod
    def take(values, indices):
        source = fake_arrow_values(values)
        return FakeArrowArray(
            [source[index] for index in fake_arrow_values(indices)],
            physical_type=values.type,
        )


class FakePyArrow:
    @staticmethod
    def large_string():
        return "large_string"

    @staticmethod
    def array(values, *, type=None):
        return FakeArrowArray(values, physical_type=type or "large_string")


def fake_pyarrow_modules():
    # PyArrow 22 does not expose ``compute`` after only ``import pyarrow``.
    # Production code must import the submodule explicitly.
    return {
        "pyarrow": FakePyArrow,
        "pyarrow.compute": FakeArrowCompute,
    }


class FakeNativeExecutor:
    def __init__(
        self,
        function,
        *,
        process_matches=True,
        guards_match=True,
        dictionary_capacity=None,
    ):
        self.function = function
        self.process_matches = process_matches
        self.guard_result = guards_match
        self.guard_checked = False
        self.invocations = 0
        self.dictionary_capacity = dictionary_capacity
        self.invocation_columns = []

    def matches_process(self, _process):
        return self.process_matches

    def guards_match(self, _process):
        self.guard_checked = True
        return self.guard_result

    def invoke(self, columns):
        self.invocations += 1
        self.invocation_columns.append(tuple(list(column) for column in columns))
        return [self.function(value) for value in columns[0]]


class FakeCinderXJit:
    def __init__(self, *, accept=True):
        self.accept = accept
        self.compiled = set()
        self.force_calls = []
        self.suppressed = set()

    def force_compile(self, function):
        self.force_calls.append(function)
        if self.accept:
            self.compiled.add(id(function))
        return self.accept

    def is_jit_compiled(self, function):
        return id(function) in self.compiled

    def jit_suppress(self, function):
        self.suppressed.add(function)
        return function


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

    def test_boundary_requires_an_executable_backend_not_typed_semantics(self):
        func = SimpleNamespace(return_dtype="string")
        self.assertFalse(
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
        self.assertFalse(
            columnar_boundary_proven(
                func, receiver_trampoline(split_strip_join)
            )
        )

    def test_boundary_accepts_guarded_value_cache_backend(self):
        self.assertTrue(
            columnar_boundary_proven(
                SimpleNamespace(return_dtype="string"),
                receiver_trampoline(_render_frozen_record),
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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
        with mock.patch.dict(sys.modules, fake_pyarrow_modules()):
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

    def test_dictionary_domain_invokes_only_first_occurrence_unique_values(self):
        scalar_calls = []
        target_calls = []

        def scalar_receiver(_receiver, value):
            scalar_calls.append(value)
            return value

        def target(value):
            target_calls.append(value)
            return value.upper()

        wrapper = make_batch_wrapper(scalar_receiver)
        executor = FakeNativeExecutor(target, dictionary_capacity=16)
        wrapper._native_batch_executor = executor
        values = ["a", "b", "a", "a", "b", "a"]
        array = GuardOrderedArrowArray(values, executor)
        with (
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
            mock.patch.dict(
                os.environ,
                {
                    "UDFJIT_COLUMNAR_DICTIONARY": "1",
                    "UDFJIT_COLUMNAR_DIAGNOSTIC_DIR": "",
                },
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.columnar."
                "time.perf_counter_ns",
                side_effect=AssertionError("timer used with diagnostics off"),
            ),
        ):
            result = wrapper(object(), FakeSeries(array))

        self.assertEqual(result.to_pylist(), ["A", "B", "A", "A", "B", "A"])
        self.assertEqual(target_calls, ["a", "b"])
        self.assertEqual(executor.invocation_columns, [(["a", "b"],)])
        self.assertEqual(array.dictionary_encode_calls, 1)
        self.assertEqual(scalar_calls, [])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["dictionary_batches"], 1)
        self.assertEqual(counters["dictionary_rows"], 6)
        self.assertEqual(counters["dictionary_unique_values"], 2)
        self.assertEqual(counters["dictionary_python_unique_rows"], 2)
        self.assertEqual(counters["dictionary_python_output_rows"], 2)
        self.assertEqual(counters["full_python_materializations"], 0)
        self.assertEqual(counters["full_python_materialized_rows"], 0)
        self.assertEqual(counters["native_batch_rows"], 6)
        self.assertEqual(counters["vector_batches"], 0)
        self.assertEqual(counters["dictionary_encode_ns"], 0)
        self.assertEqual(counters["dictionary_target_ns"], 0)

    def test_dictionary_toggle_zero_preserves_full_materialization_path(self):
        wrapper = make_batch_wrapper()
        executor = FakeNativeExecutor(str.upper, dictionary_capacity=16)
        wrapper._native_batch_executor = executor
        values = ["a", "b", "a", "a", "b", "a"]
        with (
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
            mock.patch.dict(
                os.environ,
                {"UDFJIT_COLUMNAR_DICTIONARY": "0"},
            ),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(values)))

        self.assertEqual(result.to_pylist(), [value.upper() for value in values])
        self.assertEqual(executor.invocation_columns, [(values,)])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["dictionary_batches"], 0)
        self.assertEqual(counters["dictionary_disabled_batches"], 1)
        self.assertEqual(counters["full_python_materializations"], 2)
        self.assertEqual(counters["full_python_materialized_rows"], 12)

    def test_dictionary_domain_is_enabled_by_default_at_batch_boundary(self):
        wrapper = make_batch_wrapper()
        executor = FakeNativeExecutor(str.upper, dictionary_capacity=16)
        wrapper._native_batch_executor = executor
        with (
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            result = wrapper(
                object(),
                FakeSeries(FakeArrowArray(["a", "a", "a", "a"])),
            )
        self.assertEqual(result.to_pylist(), ["A"] * 4)
        self.assertEqual(executor.invocation_columns, [(["a"],)])
        self.assertEqual(snapshot_columnar_counters()["dictionary_batches"], 1)

    def test_dictionary_domain_refuses_null_without_semantic_weakening(self):
        wrapper = make_batch_wrapper()
        executor = FakeNativeExecutor(
            lambda value: "NULL-CALLED" if value is None else value.upper(),
            dictionary_capacity=16,
        )
        wrapper._native_batch_executor = executor
        values = ["a", None, "a", None]
        with (
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
            mock.patch.dict(
                os.environ,
                {"UDFJIT_COLUMNAR_DICTIONARY": "1"},
            ),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(values)))
        self.assertEqual(
            result.to_pylist(),
            ["A", "NULL-CALLED", "A", "NULL-CALLED"],
        )
        self.assertEqual(executor.invocation_columns, [(values,)])
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["dictionary_batches"], 0)
        self.assertEqual(counters["dictionary_unavailable_batches"], 1)
        self.assertEqual(counters["full_python_materializations"], 2)

    def test_dictionary_diagnostics_record_each_phase_only_when_enabled(self):
        wrapper = make_batch_wrapper()
        wrapper._native_batch_executor = FakeNativeExecutor(
            str.upper,
            dictionary_capacity=16,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict(sys.modules, fake_pyarrow_modules()),
                mock.patch.dict(
                    os.environ,
                    {
                        "UDFJIT_COLUMNAR_DICTIONARY": "1",
                        "UDFJIT_COLUMNAR_DIAGNOSTIC_DIR": directory,
                    },
                ),
            ):
                wrapper(
                    object(),
                    FakeSeries(FakeArrowArray(["a", "b", "a", "a"])),
                )
        counters = snapshot_columnar_counters()
        for name in (
            "dictionary_encode_ns",
            "dictionary_unique_materialize_ns",
            "dictionary_target_ns",
            "dictionary_reconstruct_ns",
        ):
            self.assertGreater(counters[name], 0)

    def test_dictionary_domain_refuses_all_unique_and_capacity_miss(self):
        for values, capacity in (
            (["a", "b", "c", "d"], 16),
            (["a", "b", "c", "a", "b", "c"], 2),
        ):
            with self.subTest(values=values, capacity=capacity):
                reset_columnar_counters_for_testing()
                wrapper = make_batch_wrapper()
                executor = FakeNativeExecutor(str.upper, dictionary_capacity=capacity)
                wrapper._native_batch_executor = executor
                with (
                    mock.patch.dict(sys.modules, fake_pyarrow_modules()),
                    mock.patch.dict(
                        os.environ,
                        {"UDFJIT_COLUMNAR_DICTIONARY": "1"},
                    ),
                ):
                    result = wrapper(object(), FakeSeries(FakeArrowArray(values)))
                self.assertEqual(
                    result.to_pylist(),
                    [value.upper() for value in values],
                )
                self.assertEqual(executor.invocation_columns, [(values,)])
                counters = snapshot_columnar_counters()
                self.assertEqual(counters["dictionary_batches"], 0)
                self.assertEqual(counters["dictionary_unavailable_batches"], 1)
                self.assertEqual(counters["full_python_materializations"], 2)

    def test_dictionary_domain_handles_sliced_chunked_and_dictionary_inputs(self):
        inputs = (
            FakeChunkedArray(
                (
                    FakeArrowArray(["unused", "a", "b"], offset=3).slice(1),
                    FakeArrowArray(["a", "a"]),
                )
            ),
            FakeDictionaryArray(["a", "b"], [0, 1, 0, 0], offset=7),
        )
        for array in inputs:
            with self.subTest(physical_type=str(array.type)):
                reset_columnar_counters_for_testing()
                wrapper = make_batch_wrapper()
                executor = FakeNativeExecutor(str.upper, dictionary_capacity=16)
                wrapper._native_batch_executor = executor
                with (
                    mock.patch.dict(sys.modules, fake_pyarrow_modules()),
                    mock.patch.dict(
                        os.environ,
                        {"UDFJIT_COLUMNAR_DICTIONARY": "1"},
                    ),
                ):
                    result = wrapper(object(), FakeSeries(array))
                self.assertEqual(result.to_pylist(), ["A", "B", "A", "A"])
                self.assertEqual(executor.invocation_columns, [(["a", "b"],)])
                self.assertEqual(
                    snapshot_columnar_counters()["full_python_materializations"],
                    0,
                )

    def test_dictionary_guard_miss_precedes_encode_and_falls_back_once(self):
        scalar_calls = []

        def scalar_receiver(_receiver, value):
            scalar_calls.append(value)
            return value.upper()

        wrapper = make_batch_wrapper(scalar_receiver)
        executor = FakeNativeExecutor(
            str.upper,
            guards_match=False,
            dictionary_capacity=16,
        )
        wrapper._native_batch_executor = executor
        array = GuardOrderedArrowArray(["a", "a", "a", "a"], executor)
        with (
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
            mock.patch.dict(
                os.environ,
                {"UDFJIT_COLUMNAR_DICTIONARY": "1", "UDFJIT_MODE": "off"},
            ),
        ):
            result = wrapper(object(), FakeSeries(array))
        self.assertEqual(result.to_pylist(), ["A"] * 4)
        self.assertEqual(scalar_calls, ["a"] * 4)
        self.assertEqual(executor.invocations, 0)
        self.assertEqual(array.dictionary_encode_calls, 0)
        counters = snapshot_columnar_counters()
        self.assertEqual(counters["dictionary_batches"], 0)
        self.assertEqual(counters["dictionary_unavailable_batches"], 1)

    def test_dictionary_target_exceptions_keep_unique_first_occurrence_order(self):
        for values, failure, expected_calls in (
            (["bad", "bad", "ok", "bad"], "bad", ["bad"]),
            (["a", "a", "bad", "a"], "bad", ["a", "bad"]),
        ):
            with self.subTest(values=values):
                reset_columnar_counters_for_testing()
                scalar_calls = []
                target_calls = []

                def scalar_receiver(_receiver, value):
                    scalar_calls.append(value)
                    return value

                def target(value):
                    target_calls.append(value)
                    if value == failure:
                        raise ValueError("dictionary-target-error")
                    return value.upper()

                wrapper = make_batch_wrapper(scalar_receiver)
                wrapper._native_batch_executor = FakeNativeExecutor(
                    target,
                    dictionary_capacity=16,
                )
                with (
                    mock.patch.dict(sys.modules, fake_pyarrow_modules()),
                    mock.patch.dict(
                        os.environ,
                        {"UDFJIT_COLUMNAR_DICTIONARY": "1"},
                    ),
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "dictionary-target-error",
                    ):
                        wrapper(object(), FakeSeries(FakeArrowArray(values)))
                self.assertEqual(target_calls, expected_calls)
                self.assertEqual(scalar_calls, [])
                counters = snapshot_columnar_counters()
                self.assertEqual(counters["dictionary_batches"], 1)
                self.assertEqual(counters["published_batches"], 0)
                self.assertEqual(counters["postcommit_replays"], 0)

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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
        ):
            result = wrapper(object(), FakeSeries(FakeArrowArray(["a"])))
        self.assertEqual(result.to_pylist(), ["A"])
        self.assertEqual(inherited.invocations, 0)
        self.assertEqual(rebuilt.invocations, 1)
        build.assert_called_once()

    def test_native_executor_lazy_build_is_process_serialized(self):
        wrapper = make_batch_wrapper()
        executor = FakeNativeExecutor(str.upper)

        def slow_build(*_args, **_kwargs):
            # Release the GIL long enough for a second Daft CPU thread to see
            # the same uninitialized wrapper. The production resolver must
            # still perform exactly one process-local CinderX build.
            time.sleep(0.05)
            return executor

        with mock.patch(
            "python_udf_jit.integration.daft_ray.native_batch."
            "build_native_batch_executor",
            side_effect=slow_build,
        ) as build:
            with ThreadPoolExecutor(max_workers=4) as pool:
                resolved = tuple(
                    pool.map(
                        lambda _index: wrapper._resolve_native_batch_executor()[0],
                        range(4),
                    )
                )

        self.assertEqual(resolved, (executor,) * 4)
        build.assert_called_once()

    def test_distinct_native_executor_builds_never_overlap_process_jit_state(self):
        wrappers = (make_batch_wrapper(), make_batch_wrapper())
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def slow_build(*_args, **_kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return FakeNativeExecutor(str.upper)
            finally:
                with state_lock:
                    active -= 1

        with mock.patch(
            "python_udf_jit.integration.daft_ray.native_batch."
            "build_native_batch_executor",
            side_effect=slow_build,
        ) as build:
            with ThreadPoolExecutor(max_workers=2) as pool:
                resolved = tuple(
                    pool.map(
                        lambda wrapper: wrapper._resolve_native_batch_executor()[0],
                        wrappers,
                    )
                )

        self.assertTrue(all(executor is not None for executor in resolved))
        self.assertEqual(build.call_count, 2)
        self.assertEqual(max_active, 1)

    @unittest.skipUnless(hasattr(os, "fork"), "fork is unavailable")
    def test_forked_child_resets_inherited_process_jit_locks(self):
        import python_udf_jit.integration.daft_ray.columnar as columnar_module

        held = columnar_module._NATIVE_BATCH_BUILD_LOCK
        held.acquire()
        read_fd, write_fd = os.pipe()
        try:
            child = os.fork()
            if child == 0:
                os.close(read_fd)
                try:
                    acquired = columnar_module._NATIVE_BATCH_BUILD_LOCK.acquire(
                        timeout=0.5
                    )
                    os.write(write_fd, b"1" if acquired else b"0")
                    if acquired:
                        columnar_module._NATIVE_BATCH_BUILD_LOCK.release()
                finally:
                    os.close(write_fd)
                    os._exit(0)
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(os.read(read_fd, 1), b"1")
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            held.release()
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

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
        with mock.patch.dict(sys.modules, fake_pyarrow_modules()):
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

    def test_builder_refuses_typed_semantics_without_executable_backend(self):
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
        self.assertIsNone(executor)
        self.assertEqual(jit.force_calls, [])
        self.assertEqual(jit.suppressed, set())

    def test_regex_split_plan_is_refused_without_graph_isolation_proof(self):
        wrapper = make_batch_wrapper(receiver_trampoline(split_strip_join))
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
        self.assertIsNone(executor)
        self.assertEqual(jit.force_calls, [])

    def test_builder_compile_rejection_has_no_executor(self):
        wrapper = make_batch_wrapper(receiver_trampoline(_render_frozen_record))
        jit = FakeCinderXJit(accept=False)
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
            self.assertIsNone(executor)
        finally:
            vars(_render_frozen_record).pop("__udfjit_value_cache__", None)

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
            self.assertEqual(executor.dictionary_capacity, 16_384)
        finally:
            vars(_render_frozen_record).pop("__udfjit_value_cache__", None)
            vars(_choose_location).pop("__udfjit_invariant_cache__", None)

    def test_guarded_value_plan_refuses_dictionary_domain_reuse(self):
        wrapper = make_batch_wrapper(receiver_trampoline(_index_external_record))
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
            self.assertIsNone(executor.dictionary_capacity)
        finally:
            vars(_index_external_record).pop("__udfjit_value_cache__", None)

    def test_live_target_code_drift_falls_back_before_arrow_load(self):
        wrapper = make_batch_wrapper(receiver_trampoline(_render_frozen_record))
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
        original_code = _render_frozen_record.__code__
        _render_frozen_record.__code__ = replacement_remap_text.__code__
        try:
            array = GuardOrderedArrowArray(["α"], tracking)
            with (
                mock.patch.dict(sys.modules, fake_pyarrow_modules()),
                mock.patch.dict(os.environ, {"UDFJIT_MODE": "off"}),
            ):
                result = wrapper(object(), FakeSeries(array))
        finally:
            _render_frozen_record.__code__ = original_code
            vars(_render_frozen_record).pop("__udfjit_value_cache__", None)
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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
            mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
                mock.patch.dict(sys.modules, fake_pyarrow_modules()),
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
