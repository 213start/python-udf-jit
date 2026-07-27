from __future__ import annotations

import unittest

from python_udf_jit.governance.emergency import (
    EmergencyControl,
    EmergencySnapshot,
    EmergencyTransitionError,
)


class EmergencyControlTests(unittest.TestCase):
    def test_safe_point_updates_are_monotonic_and_only_tighten(self) -> None:
        control = EmergencyControl()
        disabled = EmergencySnapshot(
            generation=1,
            disabled=True,
            revoke_credentials_through=3,
        )

        self.assertEqual(control.apply(disabled), disabled)
        self.assertEqual(control.apply(disabled), disabled)
        self.assertEqual(control.safe_point(), disabled)

        for invalid in (
            EmergencySnapshot(
                generation=0,
                disabled=True,
                revoke_credentials_through=3,
            ),
            EmergencySnapshot(
                generation=2,
                disabled=False,
                revoke_credentials_through=3,
            ),
            EmergencySnapshot(
                generation=2,
                disabled=True,
                revoke_credentials_through=2,
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(EmergencyTransitionError):
                    control.apply(invalid)

    def test_later_generation_may_revoke_more_credentials(self) -> None:
        control = EmergencyControl()
        control.apply(
            EmergencySnapshot(
                generation=1,
                disabled=False,
                revoke_credentials_through=2,
            )
        )
        latest = control.apply(
            EmergencySnapshot(
                generation=2,
                disabled=True,
                revoke_credentials_through=9,
            )
        )

        self.assertTrue(latest.disabled)
        self.assertEqual(latest.revoke_credentials_through, 9)


if __name__ == "__main__":
    unittest.main()
