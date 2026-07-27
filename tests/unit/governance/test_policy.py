from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.governance.policy import (
    PolicyError,
    PolicySnapshot,
)


class PolicySnapshotTests(unittest.TestCase):
    def test_mainline_policy_is_deeply_immutable_and_scalar_only(self) -> None:
        budgets = {"compile_concurrency": 2, "variant_limit": 4}
        flags = {
            "scalar_python": True,
            "vector": False,
            "arrow": False,
            "rfc_009": False,
            "rfc_010": False,
            "rfc_011": False,
            "rfc_012": False,
        }

        policy = PolicySnapshot(
            version="mainline-v1",
            mode_ceiling="auto",
            budgets=budgets,
            provider_flags=flags,
            observe_shadow_compile=False,
            rollout_authorized=False,
        )
        budgets["compile_concurrency"] = 99
        flags["vector"] = True

        self.assertEqual(policy.budgets["compile_concurrency"], 2)
        self.assertFalse(policy.provider_flags["vector"])
        with self.assertRaises(TypeError):
            policy.budgets["compile_concurrency"] = 3
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.version = "changed"

    def test_mainline_policy_rejects_advanced_or_unsafe_flags(self) -> None:
        with self.assertRaisesRegex(PolicyError, "advanced_provider_enabled"):
            PolicySnapshot(
                version="bad",
                mode_ceiling="auto",
                budgets={"compile_concurrency": 1},
                provider_flags={
                    "scalar_python": True,
                    "vector": True,
                    "arrow": False,
                    "rfc_009": False,
                    "rfc_010": False,
                    "rfc_011": False,
                    "rfc_012": False,
                },
            )

        with self.assertRaisesRegex(PolicyError, "budget"):
            PolicySnapshot(
                version="bad",
                mode_ceiling="auto",
                budgets={"compile_concurrency": -1},
                provider_flags={"scalar_python": True},
            )

    def test_tightening_cannot_expand_mode_budget_or_provider_access(self) -> None:
        policy = PolicySnapshot.mainline(
            version="mainline-v1",
            budgets={"compile_concurrency": 2, "variant_limit": 4},
        )
        tightened = policy.tighten(
            version="mainline-v2",
            mode_ceiling="observe",
            budgets={"compile_concurrency": 1, "variant_limit": 4},
            disable_providers=("scalar_python",),
        )

        self.assertEqual(tightened.mode_ceiling, "observe")
        self.assertFalse(tightened.provider_flags["scalar_python"])
        with self.assertRaisesRegex(PolicyError, "policy_not_tightened"):
            tightened.tighten(
                version="mainline-v3",
                mode_ceiling="auto",
                budgets={"compile_concurrency": 2, "variant_limit": 4},
            )


if __name__ == "__main__":
    unittest.main()
