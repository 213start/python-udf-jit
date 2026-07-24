from __future__ import annotations

import unittest

from python_udf_jit.runtime.guards import (
    OuterGuardError,
    OuterGuardExpectation,
    OuterGuardObservation,
    OuterGuardRejectCode,
    guard_outer_entry,
)
from python_udf_jit.runtime.layout import ProcessIdentity


class OuterGuardTest(unittest.TestCase):
    def setUp(self):
        self.process = ProcessIdentity(123, "generation-a")
        self.expected = OuterGuardExpectation(
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "3.14.3",
            "cpython-314",
            ("asimd",),
        )
        self.observed = OuterGuardObservation(
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "3.14.3",
            "cpython-314-aarch64-linux-gnu",
            ("asimd",),
            self.process,
        )

    def test_complete_outer_guard_accepts_exact_observation(self):
        guard_outer_entry(self.expected, self.observed, expected_process=self.process)

    def test_each_machine_entry_dimension_has_a_stable_reject_code(self):
        cases = (
            ("artifact_content_sha256", "0" * 64, OuterGuardRejectCode.ARTIFACT_MISMATCH),
            ("experiment_manifest_sha256", "0" * 64, OuterGuardRejectCode.MANIFEST_MISMATCH),
            ("semantic_hash", "0" * 64, OuterGuardRejectCode.SEMANTIC_MISMATCH),
            ("schema_fingerprint", "0" * 64, OuterGuardRejectCode.SCHEMA_MISMATCH),
            ("callable_code_sha256", "0" * 64, OuterGuardRejectCode.CALLABLE_MISMATCH),
            ("target_python", "3.14.4", OuterGuardRejectCode.TARGET_PYTHON_MISMATCH),
            ("target_soabi", "cpython-315-aarch64-linux-gnu", OuterGuardRejectCode.TARGET_SOABI_MISMATCH),
            ("cpu_features", ("sve",), OuterGuardRejectCode.CPU_FEATURE_MISMATCH),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                document = dict(self.observed.__dict__)
                document[field] = value
                with self.assertRaises(OuterGuardError) as raised:
                    guard_outer_entry(
                        self.expected,
                        OuterGuardObservation(**document),
                        expected_process=self.process,
                    )
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
