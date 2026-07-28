from __future__ import annotations

import json
import sys
import unittest

from python_udf_jit.compiler.abstract_interpreter import (
    CapturedProgram,
    analyze_function,
)
from python_udf_jit.compiler.call_models import CallKind, Effect
from python_udf_jit.compiler.capture import capture_program


def affine(value):
    return value * 2.0 + 3.0


def opaque_middle(value):
    prefix = value * 2.0
    print(prefix)
    return prefix + 1.0


def controlled_calls(value):
    text = str(value).strip()
    return (abs(value), text, [value, 1.0])


def exception_path(value):
    try:
        return value + 1.0
    except TypeError:
        return 0.0


def small_helper(value):
    return value + 1.0


def controlled_small_function(value):
    return small_helper(value) * 2.0


class AbstractInterpreterTest(unittest.TestCase):
    def test_straight_line_numeric_function_needs_no_python_region(self):
        program = analyze_function(affine)

        self.assertEqual(program.analysis.python_regions, ())
        self.assertTrue(
            all(
                operation.execution == "capture"
                for operation in program.analysis.operations
            )
        )
        self.assertEqual(
            program.analysis.code_sha256,
            program.identities.code.sha256,
        )

    def test_opaque_call_forms_side_effect_region_with_exact_resume(self):
        program = analyze_function(opaque_middle)
        regions = program.analysis.python_regions

        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertEqual(region.effect, Effect.SIDE_EFFECT)
        self.assertTrue(region.may_raise)
        self.assertEqual(
            region.resume_id,
            region.recompute_resume_id(program.identities.code.sha256),
        )
        self.assertGreater(region.resume_offset, region.start_offset)
        self.assertTrue(region.live_in)
        opaque_calls = [
            operation
            for operation in program.analysis.operations
            if operation.call_kind == CallKind.OPAQUE.value
        ]
        self.assertEqual(len(opaque_calls), 1)

    def test_controlled_calls_and_readonly_aggregates_stay_python_regions(self):
        program = analyze_function(controlled_calls)
        call_kinds = {
            operation.call_kind
            for operation in program.analysis.operations
            if operation.call_kind is not None
        }

        self.assertIn(CallKind.PURE_BUILTIN.value, call_kinds)
        self.assertIn(CallKind.CONTROLLED_STRING.value, call_kinds)
        self.assertTrue(program.analysis.python_regions)
        self.assertTrue(
            any(
                operation.operation == "aggregate.list"
                and operation.execution == "python_region"
                for operation in program.analysis.operations
            )
        )
        self.assertFalse(
            any(
                operation.execution in {"arrow", "vector", "batch"}
                for operation in program.analysis.operations
            )
        )

    def test_exception_regions_retain_handler_state(self):
        program = analyze_function(exception_path)
        protected = [
            region
            for region in program.analysis.python_regions
            if region.exception_state.handler_offsets
        ]

        self.assertTrue(protected)
        self.assertTrue(
            all(region.may_raise for region in protected)
        )

    def test_small_function_is_modeled_but_remains_a_python_region(self):
        program = analyze_function(controlled_small_function)
        calls = [
            operation
            for operation in program.analysis.operations
            if operation.call_kind is not None
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].call_kind,
            CallKind.SMALL_FUNCTION.value,
        )
        self.assertEqual(calls[0].execution, "python_region")

    def test_analysis_never_executes_the_user_function(self):
        calls = 0

        def profile(frame, event, _arg):
            nonlocal calls
            if event == "call" and frame.f_code is opaque_middle.__code__:
                calls += 1

        sys.setprofile(profile)
        try:
            program = capture_program(opaque_middle)
        finally:
            sys.setprofile(None)

        self.assertEqual(calls, 0)
        self.assertTrue(program.analysis.python_regions)

    def test_program_encoding_round_trips_without_source_or_names(self):
        program = analyze_function(controlled_calls)
        encoded = program.canonical_bytes()
        restored = CapturedProgram.from_document(json.loads(encoded))

        self.assertEqual(restored, program)
        self.assertNotIn(
            controlled_calls.__code__.co_filename.encode(),
            encoded,
        )
        self.assertNotIn(b"controlled_calls", encoded)
        self.assertNotIn(b"strip", encoded)

    def test_scalar_float_constants_are_canonical_and_tamper_checked(self):
        program = analyze_function(opaque_middle)

        self.assertEqual(
            program.scalar_constants,
            (2.0.hex(), 1.0.hex()),
        )
        document = program.to_document()
        document["scalar_constants"][0] = "0x1p+1"
        with self.assertRaisesRegex(
            ValueError,
            "noncanonical captured scalar constant",
        ):
            CapturedProgram.from_document(document)


if __name__ == "__main__":
    unittest.main()
