from __future__ import annotations

import math
import struct
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import (
    LogicalType,
    SemanticCoreModule,
    SemanticLiteral,
    CoreUdfModule,
    lower_capture,
    reference_execute,
)
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.compiler.reference import reference_execute_semantic
from tests.semantic_cases import multitype_semantic_module


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

    def test_semantic_literals_round_trip_without_repr_or_address(self):
        for value in (None, True, -7, 1.25, "hello", b"bytes"):
            with self.subTest(value_type=type(value).__name__):
                literal = SemanticLiteral.from_value(value)
                restored = SemanticLiteral.from_document(
                    literal.to_document()
                )
                self.assertEqual(restored, literal)
                self.assertEqual(restored.value, value)
                self.assertNotIn(" at 0x", str(restored.to_document()))

    def test_multitype_semantic_module_round_trips_and_matches_cpython(self):
        module = multitype_semantic_module()
        restored = SemanticCoreModule.from_document(module.to_document())
        row = {
            "flag": True,
            "count": 5,
            "ratio": 3.0,
            "label": "hello",
            "payload": b"x",
        }

        actual = reference_execute_semantic(restored, (row,))
        expected = (
            row["flag"],
            row["count"] + 2,
            row["ratio"] * 0.5,
            row["label"].upper(),
            row["payload"],
            row["payload"] + b"!",
            row["count"] + 2 > 2,
            [row["flag"], row["count"] + 2],
            str(row["ratio"] * 0.5),
        )

        self.assertEqual(actual, expected)
        self.assertEqual(
            {
                operation.result_type
                for operation in module.operations
            },
            {
                LogicalType.BOOL,
                LogicalType.INT64,
                LogicalType.FLOAT64,
                LogicalType.STRING,
                LogicalType.BYTES,
                LogicalType.LIST,
                LogicalType.TUPLE,
                LogicalType.OBJECT,
            },
        )

    def test_legacy_f64_capture_enters_the_semantic_pipeline(self):
        result = compile_semantic(capture(CaptureRequest(affine)))

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason_code, "verified_semantic_ir")
        self.assertEqual(
            result.executed_passes,
            ("canonicalize", "semantic_simplify"),
        )
        self.assertIsNotNone(result.core_module)
        self.assertIsNotNone(result.region_graph)
        self.assertEqual(
            result.region_graph.regions[0].provider_candidates,
            ("scalar_cinderx",),
        )
        self.assertEqual(
            reference_execute_semantic(result.core_module, (4.0,)),
            11.0,
        )

    def test_non_jit_types_never_claim_a_cinderx_region(self):
        result = compile_semantic(multitype_semantic_module())

        self.assertTrue(result.accepted)
        self.assertTrue(result.region_graph.regions)
        self.assertTrue(
            all(
                "scalar_cinderx" not in region.provider_candidates
                for region in result.region_graph.regions
            )
        )
        self.assertEqual(
            result.core_module.semantic_hash,
            multitype_semantic_module().semantic_hash,
        )


if __name__ == "__main__":
    unittest.main()
