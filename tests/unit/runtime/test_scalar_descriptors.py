from __future__ import annotations

import dataclasses
import os
import pickle
import unittest

from python_udf_jit.runtime.descriptors import (
    ACCESS_SPEC_VERSION,
    DescriptorSet,
    descriptor_for_spec,
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import (
    SUPPORTED_SCALAR_TYPES,
    ProcessIdentity,
    normalize_scalar_value,
)


class ScalarDescriptorTest(unittest.TestCase):
    def test_five_scalar_types_have_stable_address_free_fingerprints(self):
        process = ProcessIdentity(os.getpid(), "generation-a")
        fingerprints = set()
        for index, scalar_type in enumerate(
            SUPPORTED_SCALAR_TYPES,
            start=1,
        ):
            with self.subTest(scalar_type=scalar_type):
                descriptor = descriptor_for_spec(
                    scalar_input_spec(
                        scalar_type,
                        nullable=True,
                    ),
                    epoch="epoch-a",
                    access_id=f"input-{index}",
                    descriptor_generation=index,
                    process=process,
                )
                restored = type(descriptor).from_document(
                    descriptor.to_document()
                )
                self.assertEqual(
                    pickle.loads(pickle.dumps(descriptor)),
                    restored,
                )
                self.assertEqual(
                    len(descriptor.layout_fingerprint),
                    64,
                )
                fingerprints.add(descriptor.layout_fingerprint)
                serialized = repr(descriptor.to_document()).lower()
                self.assertNotIn("address", serialized)
                self.assertNotIn("pointer", serialized)
        self.assertEqual(len(fingerprints), 5)

    def test_descriptor_set_is_exact_and_process_bound(self):
        process = ProcessIdentity(os.getpid(), "generation-a")
        input_descriptor = descriptor_for_spec(
            scalar_input_spec("int64", nullable=False),
            epoch="epoch-a",
            access_id="input",
            descriptor_generation=3,
            process=process,
        )
        output_descriptor = descriptor_for_spec(
            scalar_output_spec("int64", nullable=True),
            epoch="epoch-a",
            access_id="output",
            descriptor_generation=3,
            process=process,
        )
        descriptors = DescriptorSet(
            ACCESS_SPEC_VERSION,
            input_descriptor,
            output_descriptor,
        )

        self.assertEqual(
            DescriptorSet.from_document(descriptors.to_document()),
            descriptors,
        )
        with self.assertRaises(ValueError):
            DescriptorSet(
                ACCESS_SPEC_VERSION,
                input_descriptor,
                dataclasses.replace(
                    output_descriptor,
                    process=ProcessIdentity(
                        os.getpid(),
                        "generation-b",
                    ),
                ),
            )

    def test_scalar_value_bounds_nullability_and_float32_rounding(self):
        cases = (
            ("bool", False),
            ("int32", -(1 << 31)),
            ("int32", (1 << 31) - 1),
            ("int64", -(1 << 63)),
            ("int64", (1 << 63) - 1),
            ("float32", 1.1),
            ("float64", float("inf")),
        )
        for scalar_type, value in cases:
            with self.subTest(scalar_type=scalar_type, value=value):
                normalized = normalize_scalar_value(
                    value,
                    scalar_type,
                    nullable=False,
                )
                if scalar_type == "float32":
                    self.assertNotEqual(
                        normalized.hex(),
                        value.hex(),
                    )
                else:
                    self.assertEqual(normalized, value)
        self.assertIsNone(
            normalize_scalar_value(
                None,
                "float64",
                nullable=True,
            )
        )
        with self.assertRaises(TypeError):
            normalize_scalar_value(None, "float64", nullable=False)
        with self.assertRaises(OverflowError):
            normalize_scalar_value(1 << 31, "int32", nullable=False)
        with self.assertRaises(TypeError):
            normalize_scalar_value(True, "int64", nullable=False)


if __name__ == "__main__":
    unittest.main()
