from __future__ import annotations

import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import (
    ScalarLoweringHooks,
    compile_scalar_region,
)
from python_udf_jit.provider.scalar_python.executor import (
    PreSemanticsExecutionError,
    ScalarExecutor,
)
from python_udf_jit.runtime.continuation import (
    CommitBoundary,
    CommitPhase,
)
from python_udf_jit.runtime.layout import CinderXScalarSlotBackend, LocalScalarSlotBackend


def affine(x):
    return x * 2.0 + 3.0


class _FakeCinderjit(ModuleType):
    def __init__(self):
        super().__init__("cinderjit")
        self.calls = []
        self.values = {}
        self.borrowed = set()
        self.contracts = {}

    def _udf_create_scalar_slot(self, scalar_type, nullable):
        capsule = object()
        self.calls.append(
            ("create", capsule, scalar_type, nullable)
        )
        self.contracts[capsule] = (scalar_type, nullable)
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
        self.contracts.pop(capsule, None)

    def _udf_guard_data_handle(self, capsule):
        self.calls.append(("guard", capsule))
        if capsule not in self.borrowed:
            raise RuntimeError("not borrowed")
        return capsule

    def _udf_data_load_f64(self, guarded):
        self.calls.append(("load", guarded))
        return self.values[guarded]

    def _udf_data_is_null(self, guarded):
        self.calls.append(("is_null", guarded))
        return self.values[guarded] is None

    def _udf_data_store_f64(self, guarded, value):
        self.calls.append(("store", guarded, value))
        self.values[guarded] = value
        return value

    def _udf_data_store_null(self, guarded):
        self.calls.append(("store_null", guarded))
        self.values[guarded] = None


class ExecutorTest(unittest.TestCase):
    def test_guarded_setup_failure_stays_before_explicit_commit(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        registry.release(input_handle)
        boundary = CommitBoundary()
        invoked = False

        def compiled(_input, _output):
            nonlocal invoked
            invoked = True

        with self.assertRaisesRegex(
            PreSemanticsExecutionError,
            "slot_setup_failed",
        ):
            ScalarExecutor(registry).execute_guarded(
                compiled,
                input_handle,
                output_handle,
                1.0,
                boundary=boundary,
            )

        self.assertFalse(invoked)
        self.assertIs(boundary.phase, CommitPhase.PRE_COMMIT)
        registry.release(output_handle)

    def test_guarded_compiled_failure_is_post_commit_and_never_reclassified(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        boundary = CommitBoundary()

        def compiled(_input, _output):
            raise ArithmeticError("semantic failure")

        with self.assertRaisesRegex(ArithmeticError, "semantic failure"):
            ScalarExecutor(registry).execute_guarded(
                compiled,
                input_handle,
                output_handle,
                1.0,
                boundary=boundary,
            )

        self.assertIs(boundary.phase, CommitPhase.COMMITTED)
        registry.release(output_handle)
        registry.release(input_handle)

    def test_exception_always_releases_synchronous_borrow(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        executor = ScalarExecutor(registry)

        def fail_after_guard(candidate, _output):
            registry.guard_data_handle(candidate)
            raise ArithmeticError("semantic failure")

        with self.assertRaisesRegex(ArithmeticError, "semantic failure"):
            executor.execute(
                fail_after_guard,
                input_handle,
                output_handle,
                1.0,
            )

        with registry.borrow(input_handle) as borrowed:
            borrowed.write_f64(2.0)
            guarded = registry.guard_data_handle(input_handle)
            self.assertEqual(registry.data_load_f64(guarded), 2.0)
        with registry.borrow(output_handle) as borrowed:
            borrowed.write_f64(3.0)
        registry.release(output_handle)
        registry.release(input_handle)

    def test_executor_rejects_non_float_before_invoking_compiled_callable(self):
        registry = CapabilityRegistry(epoch="epoch-a")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        executor = ScalarExecutor(registry)
        invoked = False

        def compiled(_input, _output):
            nonlocal invoked
            invoked = True
            return 1.0

        with self.assertRaises(TypeError):
            executor.execute(
                compiled,
                input_handle,
                output_handle,
                1,
            )
        self.assertFalse(invoked)
        registry.release(output_handle)
        registry.release(input_handle)

    def test_cinderx_capsule_seam_is_borrow_scoped_and_not_claimed_as_jit(self):
        fake = _FakeCinderjit()
        with patch.dict(sys.modules, {"cinderjit": fake}):
            registry = CapabilityRegistry(epoch="epoch-a")
            input_handle = registry.register(
                CinderXScalarSlotBackend()
            )
            output_handle = registry.register(
                CinderXScalarSlotBackend()
            )
            module = lower_capture(capture(CaptureRequest(affine)))
            compiled = compile_scalar_region(
                module,
                form_verified_region(module),
                hooks=ScalarLoweringHooks(
                    fake._udf_guard_data_handle,
                    fake._udf_data_is_null,
                    fake._udf_data_load_f64,
                    fake._udf_data_store_f64,
                    fake._udf_data_store_null,
                ),
            )

            result = ScalarExecutor(registry).execute(
                compiled,
                input_handle,
                output_handle,
                2.0,
            )

            self.assertEqual(result, 7.0)
            self.assertEqual(compiled.argument_kind, "backend_pair")
            self.assertEqual(compiled.execution_mode, "python-interpreter")
            self.assertEqual(
                [call[0] for call in fake.calls],
                [
                    "create",
                    "begin",
                    "create",
                    "begin",
                    "set",
                    "guard",
                    "load",
                    "guard",
                    "store",
                    "guard",
                    "is_null",
                    "load",
                    "end",
                    "end",
                ],
            )
            self.assertNotIn(
                "capsule",
                repr(input_handle.to_document()).lower(),
            )
            registry.release(output_handle)
            registry.release(input_handle)
            self.assertEqual(fake.calls[-1][0], "release")


if __name__ == "__main__":
    unittest.main()
