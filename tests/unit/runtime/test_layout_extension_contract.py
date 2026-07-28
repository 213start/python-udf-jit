from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.runtime.descriptors import (
    LayoutRejectCode,
    admit_access_spec,
    scalar_input_spec,
)


class LayoutExtensionContractTest(unittest.TestCase):
    def test_vector_batch_and_unknown_layouts_are_stably_rejected(self):
        scalar = scalar_input_spec("float64", nullable=False)
        cases = (
            (
                "arrow_array",
                LayoutRejectCode.ARROW_LAYOUT_NOT_IMPLEMENTED.value,
            ),
            (
                "batch_view",
                LayoutRejectCode.BATCH_LAYOUT_NOT_IMPLEMENTED.value,
            ),
            (
                "future_layout",
                LayoutRejectCode.UNKNOWN_LAYOUT_KIND.value,
            ),
        )
        for layout_kind, reason in cases:
            with self.subTest(layout_kind=layout_kind):
                decision = admit_access_spec(
                    dataclasses.replace(
                        scalar,
                        layout_kind=layout_kind,
                    )
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, reason)

    def test_scalar_contract_rejects_version_capacity_and_access_drift(self):
        scalar = scalar_input_spec("float64", nullable=False)
        cases = (
            (
                {"schema_version": 2},
                LayoutRejectCode.DESCRIPTOR_VERSION_MISMATCH.value,
            ),
            (
                {"capacity": 2},
                LayoutRejectCode.CAPACITY_UNSUPPORTED.value,
            ),
            (
                {"access_mode": "write"},
                LayoutRejectCode.OWNERSHIP_ACCESS_MISMATCH.value,
            ),
            (
                {"scalar_type": "string"},
                LayoutRejectCode.SCALAR_TYPE_UNSUPPORTED.value,
            ),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                decision = admit_access_spec(
                    dataclasses.replace(scalar, **changes)
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, reason)


if __name__ == "__main__":
    unittest.main()
