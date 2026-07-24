from __future__ import annotations

import functools
import platform
import sys
import unittest

from python_udf_jit.compiler.capture import (
    CaptureRejectCode,
    CaptureRejected,
    CaptureRequest,
    capture,
    try_capture,
)


def affine(x):
    return x * 2.0 + 3.0


class CaptureTest(unittest.TestCase):
    def test_capture_is_static_and_does_not_execute_user_code(self):
        calls = 0

        def profile(frame, event, _arg):
            nonlocal calls
            if event == "call" and frame.f_code is affine.__code__:
                calls += 1

        sys.setprofile(profile)
        try:
            captured = capture(CaptureRequest(affine))
        finally:
            sys.setprofile(None)

        self.assertEqual(calls, 0)
        self.assertEqual(captured.parameter_name, "x")
        self.assertEqual(captured.input_type, "float64")
        self.assertEqual(captured.output_type, "float64")
        self.assertEqual(
            [instruction.op for instruction in captured.instructions],
            ["arg.load", "const.f64", "mul.f64", "const.f64", "add.f64", "return"],
        )
        self.assertEqual(captured.target_python, "3.14.3")
        self.assertEqual(captured.capture_runtime_python, platform.python_version())

    def test_accepts_a_single_daft_style_wrapping_layer(self):
        @functools.wraps(affine)
        def daft_method(*args, **kwargs):
            return affine(*args, **kwargs)

        class FakeFunc:
            pass

        func = FakeFunc()
        func._method = daft_method

        captured = capture(CaptureRequest(func))

        self.assertEqual(captured.fallback_identity.qualname, affine.__qualname__)

    def test_rejections_are_stable_and_never_execute_user_code(self):
        cases = []

        def division(x):
            return x / 2.0

        cases.append((division, CaptureRejectCode.UNSUPPORTED_OPERATOR))

        def comparison(x):
            return x > 2.0

        cases.append((comparison, CaptureRejectCode.UNSUPPORTED_OPERATOR))

        def opaque_call(x):
            return abs(x)

        cases.append((opaque_call, CaptureRejectCode.OPAQUE_CALL))

        def branch(x):
            if x > 0.0:
                return x
            return 0.0

        cases.append((branch, CaptureRejectCode.CONTROL_FLOW))

        constant = 2.0

        def closure(x):
            return x * constant

        cases.append((closure, CaptureRejectCode.CLOSURE_DEPENDENCY))

        global GLOBAL_SCALE
        GLOBAL_SCALE = 2.0

        def global_dependency(x):
            return x * GLOBAL_SCALE

        cases.append((global_dependency, CaptureRejectCode.GLOBAL_DEPENDENCY))

        for function, expected_code in cases:
            with self.subTest(function=function.__name__):
                result = try_capture(CaptureRequest(function))
                self.assertFalse(result.supported)
                self.assertEqual(result.reject_code, expected_code)

    def test_rejects_non_float64_schema_before_inspecting_callable(self):
        class HostileCallable:
            def __getattribute__(self, name):
                raise AssertionError(f"callable metadata inspected: {name}")

        with self.assertRaises(CaptureRejected) as raised:
            capture(
                CaptureRequest(
                    HostileCallable(),
                    input_types=("int64",),
                    output_type="float64",
                )
            )

        self.assertEqual(raised.exception.code, CaptureRejectCode.SCHEMA_MISMATCH)

    def test_supported_schema_rejects_hostile_object_without_dispatching_to_it(self):
        lookups: list[str] = []

        class HostileCallable:
            def __getattribute__(self, name):
                lookups.append(name)
                raise AssertionError(f"user override invoked: {name}")

        with self.assertRaises(CaptureRejected) as raised:
            capture(CaptureRequest(HostileCallable()))

        self.assertEqual(raised.exception.code, CaptureRejectCode.UNSUPPORTED_CALLABLE)
        self.assertEqual(lookups, [])

    def test_rejects_null_and_unknown_opcode_shapes(self):
        def null_value(x):
            return None

        def attribute(x):
            return x.real

        for function, code in (
            (null_value, CaptureRejectCode.INVALID_CONSTANT),
            (attribute, CaptureRejectCode.UNSUPPORTED_OPCODE),
        ):
            with self.subTest(function=function.__name__):
                with self.assertRaises(CaptureRejected) as raised:
                    capture(CaptureRequest(function))
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
