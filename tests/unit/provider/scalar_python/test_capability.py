from __future__ import annotations

import dataclasses
import gc
import os
import pickle
import weakref
import unittest

from python_udf_jit.provider.scalar_python.capability import (
    CapabilityError,
    CapabilityHandle,
    CapabilityRegistry,
    CapabilityRejectCode,
)
from python_udf_jit.runtime.layout import LocalScalarSlotBackend


class _Marker:
    pass


class _KeepaliveBackend(LocalScalarSlotBackend):
    def __init__(self):
        super().__init__()
        self.marker = _Marker()

    def borrow_keepalive(self):
        return self.marker


class CapabilityTest(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry(epoch="epoch-a")
        self.backend = LocalScalarSlotBackend()
        self.handle = self.registry.register(self.backend, access_id="reusable-slot")

    def tearDown(self):
        try:
            self.registry.release(self.handle)
        except CapabilityError:
            pass

    def assert_rejected(self, handle, code):
        with self.assertRaises(CapabilityError) as raised:
            with self.registry.borrow(handle):
                pass
        self.assertEqual(raised.exception.code, code)

    def test_forged_or_guessed_token_and_generation_are_rejected(self):
        self.assert_rejected(
            dataclasses.replace(self.handle, token="0" * len(self.handle.token)),
            CapabilityRejectCode.TOKEN_MISMATCH,
        )
        self.assert_rejected(
            dataclasses.replace(self.handle, generation=self.handle.generation + 1),
            CapabilityRejectCode.GENERATION_MISMATCH,
        )

    def test_handle_round_trip_contains_authority_but_no_storage_address(self):
        document = self.handle.to_document()

        self.assertEqual(
            CapabilityHandle.from_document(document),
            pickle.loads(pickle.dumps(self.handle)),
        )
        serialized = repr(document).lower()
        self.assertNotIn("address", serialized)
        self.assertNotIn("pointer", serialized)
        self.assertNotIn("capsule", serialized)

    def test_release_and_reuse_rejects_old_handle_without_aba(self):
        old = self.handle
        self.registry.release(old)
        self.handle = self.registry.register(LocalScalarSlotBackend(), access_id="reusable-slot")

        self.assertGreater(self.handle.generation, old.generation)
        self.assertNotEqual(self.handle.token, old.token)
        self.assert_rejected(old, CapabilityRejectCode.GENERATION_MISMATCH)

    def test_handle_is_bound_to_registry_process_generation_and_pid(self):
        other = CapabilityRegistry(epoch="epoch-a")
        with self.assertRaises(CapabilityError) as raised:
            with other.borrow(self.handle):
                pass
        self.assertEqual(raised.exception.code, CapabilityRejectCode.REGISTRY_MISMATCH)

        forged_generation = dataclasses.replace(
            self.handle, process_generation="another-process-generation"
        )
        self.assert_rejected(forged_generation, CapabilityRejectCode.PROCESS_GENERATION_MISMATCH)

        child = os.fork()
        if child == 0:
            try:
                with self.registry.borrow(self.handle):
                    pass
            except CapabilityError as error:
                os._exit(0 if error.code == CapabilityRejectCode.PROCESS_MISMATCH else 2)
            os._exit(3)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_borrow_lifetime_dominates_write_guard_and_load(self):
        with self.registry.borrow(self.handle) as borrowed:
            borrowed.write_f64(4.25)
            guarded = self.registry.guard_data_handle(self.handle)
            self.assertEqual(self.registry.data_load_f64(guarded), 4.25)

        with self.assertRaises(CapabilityError) as raised:
            borrowed.write_f64(5.0)
        self.assertEqual(raised.exception.code, CapabilityRejectCode.NOT_BORROWED)
        with self.assertRaises(CapabilityError) as raised:
            self.registry.data_load_f64(guarded)
        self.assertEqual(raised.exception.code, CapabilityRejectCode.NOT_BORROWED)

    def test_each_borrow_requires_rebinding_the_current_row(self):
        with self.registry.borrow(self.handle) as borrowed:
            borrowed.write_f64(4.25)
            guarded = self.registry.guard_data_handle(self.handle)
            self.assertEqual(self.registry.data_load_f64(guarded), 4.25)

        with self.registry.borrow(self.handle):
            guarded = self.registry.guard_data_handle(self.handle)
            with self.assertRaisesRegex(RuntimeError, "has not been initialized"):
                self.registry.data_load_f64(guarded)

    def test_explicit_keepalive_is_held_only_for_active_borrow(self):
        self.registry.release(self.handle)
        backend = _KeepaliveBackend()
        self.handle = self.registry.register(backend, access_id="keepalive-slot")

        with self.registry.borrow(self.handle):
            reference = weakref.ref(backend.marker)
            backend.marker = None
            gc.collect()
            self.assertIsNotNone(reference())

        gc.collect()
        self.assertIsNone(reference())


if __name__ == "__main__":
    unittest.main()
