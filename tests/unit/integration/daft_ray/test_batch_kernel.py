from __future__ import annotations

import pickle
import unittest

from python_udf_jit.integration.daft_ray.batch_kernel import (
    CallableBatchKernel,
    build_batch_kernel,
    register_batch_kernel,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.wrapper import (
    BatchExecutionWrapper,
    FallbackOnlyWrapper,
)


class FakeSeries:
    def __init__(self, values):
        self.values = values

    def to_pylist(self):
        return list(self.values)


def scalar(value: int) -> int:
    return value * 2


def batch(values: list[int]) -> list[int]:
    return [value * 2 for value in values]


def bad_length(_values: list[int]) -> list[int]:
    return []


class BatchKernelTest(unittest.TestCase):
    def make_scalar_wrapper(self, function=scalar):
        return FallbackOnlyWrapper(
            candidate_id="candidate-batch",
            original_callable=function,
            carrier=ProductionCarrierState.placeholder(
                "candidate-batch",
                "a" * 64,
            ),
        )

    def test_explicit_kernel_is_found_through_a_transparent_closure(self):
        register_batch_kernel(scalar, batch, kind="test_vectorized")

        def make_outer(function):
            def outer(value):
                return function(value)

            return outer

        kernel = build_batch_kernel(make_outer(scalar))

        self.assertIsInstance(kernel, CallableBatchKernel)
        self.assertEqual(kernel.kind, "test_vectorized")
        self.assertEqual(kernel.invoke([1, 2, 3]), [2, 4, 6])

    def test_batch_wrapper_invokes_kernel_once_for_all_rows(self):
        calls = []

        def recording(values):
            calls.append(list(values))
            return [value * 2 for value in values]

        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_vectorized", recording),
        )

        self.assertEqual(wrapper(None, FakeSeries([1, 2, 3])), [2, 4, 6])
        self.assertEqual(calls, [[1, 2, 3]])

    def test_explicit_kernel_failure_is_not_silently_replayed_rowwise(self):
        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_bad", bad_length),
        )

        with self.assertRaisesRegex(ValueError, "output_length_mismatch"):
            wrapper(None, FakeSeries([1, 2, 3]))

    def test_batch_wrapper_and_explicit_kernel_are_pickle_stable(self):
        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_vectorized", batch),
        )

        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertEqual(restored(None, FakeSeries([4, 5])), [8, 10])
        self.assertEqual(restored.batch_kernel.kind, "test_vectorized")


if __name__ == "__main__":
    unittest.main()
