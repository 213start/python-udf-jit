from __future__ import annotations

import dataclasses
import random
import unittest

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.capture_verifier import (
    CaptureVerificationError,
    CaptureVerificationRejectCode,
    verify_captured_program,
)


def two_breaks(value):
    first = value + 1.0
    print(first)
    second = first * 2.0
    print(second)
    return second - 3.0


def branch_shape(value):
    if value is None:
        return None
    if value > 0.0:
        return (value, "positive")
    return [value, "nonpositive"]


class GraphBreakTest(unittest.TestCase):
    def test_two_opaque_calls_have_stable_nonoverlapping_regions(self):
        first = analyze_function(two_breaks)
        second = analyze_function(two_breaks)

        self.assertEqual(first, second)
        self.assertEqual(len(first.analysis.python_regions), 2)
        previous_end = -1
        for region in first.analysis.python_regions:
            self.assertGreaterEqual(region.start_offset, previous_end)
            self.assertGreater(region.end_offset, region.start_offset)
            self.assertEqual(region.end_offset, region.resume_offset)
            previous_end = region.end_offset

    def test_verifier_rejects_resume_live_and_coverage_corruption(self):
        program = analyze_function(two_breaks)
        region = program.analysis.python_regions[0]
        corrupt_regions = (
            dataclasses.replace(region, resume_id="0" * 64),
            dataclasses.replace(region, live_in=("not-a-value",)),
        )
        expected_codes = (
            CaptureVerificationRejectCode.RESUME_ID,
            CaptureVerificationRejectCode.LIVE_VALUE,
        )
        for corrupted, expected in zip(
            corrupt_regions,
            expected_codes,
            strict=True,
        ):
            analysis = dataclasses.replace(
                program.analysis,
                python_regions=(
                    corrupted,
                    *program.analysis.python_regions[1:],
                ),
            )
            changed = dataclasses.replace(program, analysis=analysis)
            with self.assertRaises(CaptureVerificationError) as raised:
                verify_captured_program(changed)
            self.assertEqual(raised.exception.code, expected)

        missing = dataclasses.replace(
            program,
            analysis=dataclasses.replace(
                program.analysis,
                python_regions=program.analysis.python_regions[1:],
            ),
        )
        with self.assertRaises(CaptureVerificationError) as raised:
            verify_captured_program(missing)
        self.assertEqual(
            raised.exception.code,
            CaptureVerificationRejectCode.REGION_COVERAGE,
        )

    def test_static_capture_does_not_change_cpython_results(self):
        before = analyze_function(branch_shape)
        generator = random.Random(20260727)
        inputs = [None, 0.0, -0.0, float("inf"), float("-inf")]
        inputs.extend(generator.uniform(-1e6, 1e6) for _ in range(10_000))
        expected = [branch_shape(value) for value in inputs]

        after = analyze_function(branch_shape)
        actual = [branch_shape(value) for value in inputs]

        self.assertEqual(before, after)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
