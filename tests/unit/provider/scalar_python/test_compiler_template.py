from __future__ import annotations

import dis
import math
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import compile_scalar_region
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.layout import LocalScalarSlotBackend


def affine(x):
    return x * 2.0 + 3.0


def shifted_product(x):
    return x * 4.0 - 1.5


def changed_constant(x):
    return x * 2.0 + 4.0


def changed_operator(x):
    return x * 2.0 - 3.0


class CompilerTemplateTest(unittest.TestCase):
    def compile(self, function):
        module = lower_capture(capture(CaptureRequest(function)))
        region = form_verified_region(module)
        registry = CapabilityRegistry(epoch="epoch-a")
        compiled = compile_scalar_region(module, region, registry=registry)
        backend = LocalScalarSlotBackend()
        handle = registry.register(backend)
        return module, registry, compiled, handle

    def test_interpreter_template_is_driven_by_verified_region(self):
        module, registry, compiled, handle = self.compile(affine)
        executor = ScalarExecutor(registry)

        for value in (0.0, -0.0, 1.25, float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                actual = executor.execute(compiled, handle, value)
                expected = affine(value)
                if math.isnan(expected):
                    self.assertTrue(math.isnan(actual))
                else:
                    self.assertEqual(actual.hex(), expected.hex())

        self.assertEqual(compiled.semantic_hash, module.semantic_hash)
        self.assertEqual(compiled.execution_mode, "python-interpreter")
        names = compiled.code_object.co_names
        self.assertIn("_udf_guard_data_handle", names)
        self.assertIn("_udf_data_load_f64", names)
        self.assertIn("BINARY_OP", {instruction.opname for instruction in dis.get_instructions(compiled.code_object)})
        registry.release(handle)

    def test_guard_completes_before_data_load_is_called(self):
        module = lower_capture(capture(CaptureRequest(affine)))
        region = form_verified_region(module)
        calls = []

        def guard(handle):
            calls.append(("guard", handle))
            return "guarded"

        def load(guarded):
            calls.append(("load", guarded))
            return 2.0

        compiled = compile_scalar_region(
            module,
            region,
            guard_function=guard,
            load_function=load,
        )

        self.assertEqual(compiled("handle"), 7.0)  # type: ignore[arg-type]
        self.assertEqual(calls, [("guard", "handle"), ("load", "guarded")])

    def test_each_constant_or_operator_change_changes_code_hash_and_result(self):
        _, registry_a, compiled_a, handle_a = self.compile(affine)
        _, registry_b, compiled_b, handle_b = self.compile(changed_constant)
        _, registry_c, compiled_c, handle_c = self.compile(changed_operator)

        result_a = ScalarExecutor(registry_a).execute(compiled_a, handle_a, 2.0)
        result_b = ScalarExecutor(registry_b).execute(compiled_b, handle_b, 2.0)
        result_c = ScalarExecutor(registry_c).execute(compiled_c, handle_c, 2.0)

        self.assertNotEqual(compiled_a.code_hash, compiled_b.code_hash)
        self.assertNotEqual(compiled_a.code_hash, compiled_c.code_hash)
        self.assertEqual(result_a, 7.0)
        self.assertEqual(result_b, 8.0)
        self.assertEqual(result_c, 1.0)
        registry_a.release(handle_a)
        registry_b.release(handle_b)
        registry_c.release(handle_c)


if __name__ == "__main__":
    unittest.main()
