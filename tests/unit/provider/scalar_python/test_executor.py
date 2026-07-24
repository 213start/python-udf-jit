from __future__ import annotations

import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import compile_scalar_region
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.layout import CinderXScalarSlotBackend, LocalScalarSlotBackend


def affine(x):
    return x * 2.0 + 3.0


class _FakeCinderjit(ModuleType):
    def __init__(self):
        super().__init__("cinderjit")
        self.calls = []
        self.values = {}
        self.borrowed = set()

    def _udf_create_scalar_slot(self):
        capsule = object()
        self.calls.append(("create", capsule))
        return capsule

    def _udf_set_scalar_slot(self, capsule, value):
        self.calls.append(("set", capsule, value))
        self.values[capsule] = value

    def _udf_begin_scalar_slot_borrow(self, capsule):
        self.calls.append(("begin", capsule))
        self.borrowed.add(capsule)

    def _udf_end_scalar_slot_borrow(self, capsule):
        self.calls.append(("end", capsule))
        self.borrowed.remove(capsule)

    def _udf_release_scalar_slot(self, capsule):
        self.calls.append(("release", capsule))
        self.values.pop(capsule, None)

    def _udf_guard_data_handle(self, capsule):
        self.calls.append(("guard", capsule))
        if capsule not in self.borrowed:
            raise RuntimeError("not borrowed")
        return capsule

    def _udf_data_load_f64(self, guarded):
        self.calls.append(("load", guarded))
        return self.values[guarded]


class ExecutorTest(unittest.TestCase):
    def test_exception_always_releases_synchronous_borrow(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        handle = registry.register(LocalScalarSlotBackend())
        executor = ScalarExecutor(registry)

        def fail_after_guard(candidate):
            registry.guard_data_handle(candidate)
            raise ArithmeticError("semantic failure")

        with self.assertRaisesRegex(ArithmeticError, "semantic failure"):
            executor.execute(fail_after_guard, handle, 1.0)

        with registry.borrow(handle) as borrowed:
            borrowed.write_f64(2.0)
            guarded = registry.guard_data_handle(handle)
            self.assertEqual(registry.data_load_f64(guarded), 2.0)
        registry.release(handle)

    def test_executor_rejects_non_float_before_invoking_compiled_callable(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        handle = registry.register(LocalScalarSlotBackend())
        executor = ScalarExecutor(registry)
        invoked = False

        def compiled(_candidate):
            nonlocal invoked
            invoked = True
            return 1.0

        with self.assertRaises(TypeError):
            executor.execute(compiled, handle, 1)  # type: ignore[arg-type]
        self.assertFalse(invoked)
        registry.release(handle)

    def test_cinderx_capsule_seam_is_borrow_scoped_and_not_claimed_as_jit(self):
        fake = _FakeCinderjit()
        with patch.dict(sys.modules, {"cinderjit": fake}):
            registry = CapabilityRegistry(epoch="epoch-a")
            backend = CinderXScalarSlotBackend()
            handle = registry.register(backend)
            module = lower_capture(capture(CaptureRequest(affine)))
            compiled = compile_scalar_region(
                module,
                form_verified_region(module),
                guard_function=fake._udf_guard_data_handle,
                load_function=fake._udf_data_load_f64,
            )

            result = ScalarExecutor(registry).execute(compiled, handle, 2.0)

            self.assertEqual(result, 7.0)
            self.assertEqual(compiled.argument_kind, "backend")
            self.assertEqual(compiled.execution_mode, "python-interpreter")
            self.assertEqual(
                [call[0] for call in fake.calls],
                ["create", "begin", "set", "guard", "load", "end"],
            )
            self.assertNotIn("capsule", repr(handle.to_document()).lower())
            registry.release(handle)
            self.assertEqual(fake.calls[-1][0], "release")


if __name__ == "__main__":
    unittest.main()
