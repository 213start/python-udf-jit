from __future__ import annotations

import unittest

from python_udf_jit.governance.explain import (
    build_explain_report,
    source_identity,
)
from python_udf_jit.governance.policy import PolicySnapshot
from python_udf_jit.governance.telemetry import GovernanceEvent


class RFC008UnitTests(unittest.TestCase):
    def test_rfc008_unit_contract(self) -> None:
        policy = PolicySnapshot.mainline(
            version="policy-a",
            budgets={
                "compile_concurrency": 1,
                "variant_limit": 8,
            },
            observe_shadow_compile=True,
            rollout_authorized=False,
        )
        same = PolicySnapshot.mainline(
            version="policy-a",
            budgets={
                "variant_limit": 8,
                "compile_concurrency": 1,
            },
            observe_shadow_compile=True,
            rollout_authorized=False,
        )
        self.assertEqual(policy.sha256, same.sha256)
        self.assertFalse(policy.provider_flags["vector"])
        self.assertFalse(policy.provider_flags["rfc_009"])

        identity = source_identity(
            "private.module",
            "private_function",
            "b" * 64,
        )
        event = GovernanceEvent(
            run_id="run-a",
            job_id="job-a",
            tenant_id="tenant-a",
            policy_sha256=policy.sha256,
            stage="variant",
            decision="interpret",
            reason_code="compile_inflight",
            source_identity=identity,
        )
        report = build_explain_report(
            [event],
            run_id="run-a",
            job_id="job-a",
            tenant_id="tenant-a",
            policy_sha256=policy.sha256,
        )
        encoded = repr(report)
        self.assertTrue(report["dropped_business_values"])
        self.assertNotIn("private.module", encoded)
        self.assertNotIn("private_function", encoded)
