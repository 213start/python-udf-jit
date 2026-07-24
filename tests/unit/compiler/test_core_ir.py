from __future__ import annotations

import math
import struct
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import CoreUdfModule, lower_capture, reference_execute


def affine(x):
    return x * 2.0 + 3.0


def float_bits(value: float) -> bytes:
    return struct.pack(">d", value)


class CoreIrTest(unittest.TestCase):
    def test_lowering_is_typed_pure_and_deterministic(self):
        first = lower_capture(capture(CaptureRequest(affine)))
        second = lower_capture(capture(CaptureRequest(affine)))

        self.assertEqual(first, second)
        self.assertEqual(first.semantic_hash, second.semantic_hash)
        self.assertEqual(first.input_type, "float64")
        self.assertEqual(first.output_type, "float64")
        self.assertEqual(first.effect, "pure")
        self.assertEqual(CoreUdfModule.from_document(first.to_document()), first)

    def test_reference_interpreter_matches_cpython_oracle_at_boundaries(self):
        module = lower_capture(capture(CaptureRequest(affine)))
        values = (
            0.0,
            -0.0,
            1.25,
            -1.25,
            float("nan"),
            float("inf"),
            float("-inf"),
            1.7976931348623157e308,
            5e-324,
        )

        for value in values:
            with self.subTest(value=value):
                expected = affine(value)
                actual = reference_execute(module, value)
                if math.isnan(expected):
                    self.assertTrue(math.isnan(actual))
                else:
                    self.assertEqual(float_bits(actual), float_bits(expected))


if __name__ == "__main__":
    unittest.main()
