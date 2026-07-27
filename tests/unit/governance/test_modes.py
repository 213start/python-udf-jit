from __future__ import annotations

import unittest

from python_udf_jit.governance.modes import RuntimeMode, resolve_mode
from python_udf_jit.governance.policy import PolicySnapshot


def _policy(**changes: object) -> PolicySnapshot:
    values: dict[str, object] = {
        "version": "mainline-v1",
        "mode_ceiling": "auto",
        "budgets": {"compile_concurrency": 1},
        "provider_flags": {
            "scalar_python": True,
            "vector": False,
            "arrow": False,
            "rfc_009": False,
            "rfc_010": False,
            "rfc_011": False,
            "rfc_012": False,
        },
        "observe_shadow_compile": False,
        "rollout_authorized": True,
    }
    values.update(changes)
    return PolicySnapshot(**values)


class ModeResolutionTests(unittest.TestCase):
    def test_resolution_priority_is_disable_plugin_mode_compatibility_policy(self) -> None:
        cases = (
            (
                {
                    "emergency_disabled": True,
                    "plugin_enabled": True,
                    "requested_mode": "auto",
                    "compatible": True,
                    "policy": _policy(),
                },
                "emergency_disabled",
            ),
            (
                {
                    "emergency_disabled": False,
                    "plugin_enabled": False,
                    "requested_mode": "auto",
                    "compatible": True,
                    "policy": _policy(),
                },
                "plugin_disabled",
            ),
            (
                {
                    "emergency_disabled": False,
                    "plugin_enabled": True,
                    "requested_mode": "off",
                    "compatible": False,
                    "policy": _policy(mode_ceiling="off"),
                },
                "mode_off",
            ),
            (
                {
                    "emergency_disabled": False,
                    "plugin_enabled": True,
                    "requested_mode": "auto",
                    "compatible": False,
                    "policy": _policy(mode_ceiling="off"),
                },
                "incompatible",
            ),
            (
                {
                    "emergency_disabled": False,
                    "plugin_enabled": True,
                    "requested_mode": "auto",
                    "compatible": True,
                    "policy": _policy(mode_ceiling="observe"),
                },
                "policy_mode_ceiling",
            ),
        )
        for inputs, reason in cases:
            with self.subTest(reason=reason):
                decision = resolve_mode(**inputs)
                self.assertEqual(decision.reason, reason)
                self.assertIn(decision.mode, (RuntimeMode.OFF, RuntimeMode.OBSERVE))
                self.assertFalse(decision.optimized_execution)

    def test_observe_does_not_shadow_compile_without_two_explicit_authorizations(self) -> None:
        default = resolve_mode(
            emergency_disabled=False,
            plugin_enabled=True,
            requested_mode="observe",
            compatible=True,
            policy=_policy(observe_shadow_compile=False),
            shadow_compile_requested=True,
        )
        policy_only = resolve_mode(
            emergency_disabled=False,
            plugin_enabled=True,
            requested_mode="observe",
            compatible=True,
            policy=_policy(observe_shadow_compile=True),
            shadow_compile_requested=False,
        )
        authorized = resolve_mode(
            emergency_disabled=False,
            plugin_enabled=True,
            requested_mode="observe",
            compatible=True,
            policy=_policy(observe_shadow_compile=True),
            shadow_compile_requested=True,
        )

        self.assertFalse(default.compile_enabled)
        self.assertFalse(policy_only.compile_enabled)
        self.assertTrue(authorized.compile_enabled)
        self.assertFalse(authorized.optimized_execution)

    def test_auto_requires_independent_rollout_authorization(self) -> None:
        decision = resolve_mode(
            emergency_disabled=False,
            plugin_enabled=True,
            requested_mode="auto",
            compatible=True,
            policy=_policy(rollout_authorized=False),
        )

        self.assertEqual(decision.mode, RuntimeMode.OBSERVE)
        self.assertEqual(decision.reason, "rollout_not_authorized")
        self.assertFalse(decision.optimized_execution)


if __name__ == "__main__":
    unittest.main()
