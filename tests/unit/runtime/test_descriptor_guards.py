from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.runtime.guards import DescriptorGuardError, DescriptorRejectCode, guard_descriptor
from python_udf_jit.runtime.layout import (
    FLOAT64_SCALAR_TYPE,
    SCALAR_SLOT_ABI_VERSION,
    ProcessIdentity,
    ScalarSlotDescriptor,
)


class DescriptorGuardsTest(unittest.TestCase):
    def setUp(self):
        self.process = ProcessIdentity(pid=31337, generation="generation-a")
        self.descriptor = ScalarSlotDescriptor(
            SCALAR_SLOT_ABI_VERSION,
            FLOAT64_SCALAR_TYPE,
            "epoch-a",
            "access-a",
            self.process,
        )

    def test_valid_descriptor_is_returned_unchanged(self):
        self.assertIs(
            guard_descriptor(
                self.descriptor,
                expected_epoch="epoch-a",
                expected_access_id="access-a",
                expected_process=self.process,
            ),
            self.descriptor,
        )

    def test_every_descriptor_dimension_is_guarded(self):
        cases = (
            (
                dataclasses.replace(self.descriptor, abi_version=999),
                DescriptorRejectCode.ABI_MISMATCH,
            ),
            (
                dataclasses.replace(self.descriptor, scalar_type="int64"),
                DescriptorRejectCode.TYPE_MISMATCH,
            ),
            (
                dataclasses.replace(self.descriptor, nullable=True),
                DescriptorRejectCode.NULLABILITY_MISMATCH,
            ),
            (
                dataclasses.replace(
                    self.descriptor,
                    ownership="borrowed_input",
                ),
                DescriptorRejectCode.OWNERSHIP_MISMATCH,
            ),
            (
                dataclasses.replace(
                    self.descriptor,
                    access_mode="read",
                ),
                DescriptorRejectCode.ACCESS_MODE_MISMATCH,
            ),
            (
                dataclasses.replace(
                    self.descriptor,
                    descriptor_generation=2,
                ),
                DescriptorRejectCode.GENERATION_MISMATCH,
            ),
            (
                dataclasses.replace(self.descriptor, epoch="epoch-b"),
                DescriptorRejectCode.EPOCH_MISMATCH,
            ),
            (
                dataclasses.replace(self.descriptor, access_id="access-b"),
                DescriptorRejectCode.ACCESS_MISMATCH,
            ),
            (
                dataclasses.replace(
                    self.descriptor,
                    process=ProcessIdentity(pid=31338, generation="generation-a"),
                ),
                DescriptorRejectCode.PROCESS_MISMATCH,
            ),
            (
                dataclasses.replace(
                    self.descriptor,
                    process=ProcessIdentity(pid=31337, generation="generation-b"),
                ),
                DescriptorRejectCode.PROCESS_MISMATCH,
            ),
        )
        for descriptor, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(DescriptorGuardError) as raised:
                    guard_descriptor(
                        descriptor,
                        expected_epoch="epoch-a",
                        expected_access_id="access-a",
                        expected_process=self.process,
                    )
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
