from __future__ import annotations

import pickle
import unittest

from python_udf_jit.runtime.layout import (
    FLOAT64_SCALAR_TYPE,
    SCALAR_SLOT_ABI_VERSION,
    SUPPORTED_SCALAR_TYPES,
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
            set(document),
            {
                "access_mode",
                "abi_version",
                "access_id",
                "capacity",
                "descriptor_generation",
                "epoch",
                "layout_kind",
                "nullable",
                "ownership",
                "process",
                "scalar_type",
            },
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

        values = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": (1 << 63) - 1,
            "float32": 1.25,
            "float64": -0.0,
        }
        for scalar_type in SUPPORTED_SCALAR_TYPES:
            with self.subTest(scalar_type=scalar_type):
                typed = LocalScalarSlotBackend(
                    scalar_type=scalar_type,
                    nullable=True,
                )
                typed.begin_borrow()
                typed.write_scalar(
                    values[scalar_type],
                    scalar_type=scalar_type,
                    nullable=True,
                )
                self.assertEqual(
                    typed.load_scalar(
                        scalar_type=scalar_type,
                        nullable=True,
                    ),
                    values[scalar_type],
                )
                typed.write_scalar(
                    None,
                    scalar_type=scalar_type,
                    nullable=True,
                )
                self.assertIsNone(
                    typed.load_scalar(
                        scalar_type=scalar_type,
                        nullable=True,
                    )
                )
                typed.end_borrow()
                typed.close()


if __name__ == "__main__":
    unittest.main()
