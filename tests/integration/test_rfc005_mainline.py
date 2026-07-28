from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.runtime.descriptors import (
    LayoutRejectCode,
    admit_access_spec,
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import SUPPORTED_SCALAR_TYPES
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer


class RFC005IntegrationTests(unittest.TestCase):
    def test_rfc005_integration_contract(self):
        values = {
            "bool": False,
            "int32": (1 << 31) - 1,
            "int64": -(1 << 63),
            "float32": float("inf"),
            "float64": float("nan"),
        }
        physicalizer = ScalarPhysicalizer(epoch="rfc005-integration")
        fingerprints = set()
        for scalar_type in SUPPORTED_SCALAR_TYPES:
            with physicalizer.open_call(
                scalar_input_spec(scalar_type, nullable=True),
                scalar_output_spec(scalar_type, nullable=True),
                values[scalar_type],
            ) as frame:
                loaded = frame.load_input()
                frame.stage_output(loaded)
                frame.publish_output()
                fingerprints.add(
                    frame.descriptor_set.input_descriptor.layout_fingerprint
                )
        self.assertEqual(len(fingerprints), 5)
        self.assertEqual(physicalizer.active_frame_count, 0)
        physicalizer.close()

        for layout_kind, expected in (
            (
                "arrow_array",
                LayoutRejectCode.ARROW_LAYOUT_NOT_IMPLEMENTED.value,
            ),
            (
                "batch_view",
                LayoutRejectCode.BATCH_LAYOUT_NOT_IMPLEMENTED.value,
            ),
            (
                "unknown",
                LayoutRejectCode.UNKNOWN_LAYOUT_KIND.value,
            ),
        ):
            decision = admit_access_spec(
                dataclasses.replace(
                    scalar_input_spec(
                        "float64",
                        nullable=False,
                    ),
                    layout_kind=layout_kind,
                )
            )
            self.assertFalse(decision.accepted)
            self.assertEqual(decision.reason, expected)


if __name__ == "__main__":
    unittest.main()
