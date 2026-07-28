from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.protocol.admission import (
    ComponentCapabilities,
    admit_driver_worker,
)


class ComponentAdmissionTest(unittest.TestCase):
    def test_only_exact_formal_component_contract_is_admitted(self):
        current = ComponentCapabilities.current()

        self.assertTrue(
            admit_driver_worker(current, current).accepted
        )
        for field, reason in (
            ("wheel_major", "wheel_major_mismatch"),
            ("wrapper_format", "wrapper_format_mismatch"),
            ("carrier_format", "carrier_format_mismatch"),
            (
                "artifact_format_major",
                "artifact_format_major_mismatch",
            ),
            (
                "artifact_format_minor",
                "artifact_format_minor_mismatch",
            ),
            ("runtime_abi", "runtime_abi_mismatch"),
            ("adapter_abi", "adapter_abi_mismatch"),
        ):
            worker = dataclasses.replace(
                current,
                **{field: getattr(current, field) + 1},
            )
            with self.subTest(field=field):
                decision = admit_driver_worker(current, worker)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, reason)

        for field, invalid in (
            ("wheel_major", 0),
            ("wrapper_format", True),
            ("artifact_format_minor", -1),
        ):
            with self.subTest(invalid_field=field):
                with self.assertRaises(ValueError):
                    dataclasses.replace(current, **{field: invalid})


if __name__ == "__main__":
    unittest.main()
