from __future__ import annotations

import pickle
import unittest

from python_udf_jit.runtime.layout import (
    FLOAT64_SCALAR_TYPE,
    SCALAR_SLOT_ABI_VERSION,
    LocalScalarSlotBackend,
    ProcessIdentity,
    ScalarSlotDescriptor,
)


class LayoutTest(unittest.TestCase):
    def test_descriptor_is_strictly_serializable_and_address_free(self):
        descriptor = ScalarSlotDescriptor(
            abi_version=SCALAR_SLOT_ABI_VERSION,
            scalar_type=FLOAT64_SCALAR_TYPE,
            epoch="cluster-epoch-1",
            access_id="slot-1",
            process=ProcessIdentity(pid=1234, generation="process-generation-1"),
        )

        document = descriptor.to_document()

        self.assertEqual(ScalarSlotDescriptor.from_document(document), descriptor)
        self.assertEqual(pickle.loads(pickle.dumps(descriptor)), descriptor)
        self.assertEqual(
            set(document), {"abi_version", "scalar_type", "epoch", "access_id", "process"}
        )
        serialized = repr(document).lower()
        self.assertNotIn("address", serialized)
        self.assertNotIn("pointer", serialized)
        self.assertNotIn("capsule", serialized)

    def test_local_backend_has_float64_interpreter_semantics(self):
        backend = LocalScalarSlotBackend()

        with self.assertRaises(RuntimeError):
            backend.load_f64()
        with self.assertRaises(TypeError):
            backend.write_f64(1)  # type: ignore[arg-type]

        backend.write_f64(-0.0)
        self.assertEqual(backend.load_f64().hex(), (-0.0).hex())
        backend.close()
        with self.assertRaises(RuntimeError):
            backend.load_f64()


if __name__ == "__main__":
    unittest.main()
