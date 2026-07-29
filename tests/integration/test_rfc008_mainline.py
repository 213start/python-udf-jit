from __future__ import annotations

import json
import subprocess
import sys
import unittest

from python_udf_jit.governance.modes import RuntimeMode, resolve_mode
from python_udf_jit.governance.policy import PolicySnapshot
from tests.integration.test_driver_worker_artifact_roundtrip import (
    worker_environment,
)


class RFC008IntegrationTests(unittest.TestCase):
    def test_rfc008_integration_contract(self) -> None:
        policy = PolicySnapshot.mainline(
            version="frozen-a",
            budgets={"compile_concurrency": 1, "variant_limit": 8},
            observe_shadow_compile=True,
            rollout_authorized=True,
        )
        script = r"""
import json
from python_udf_jit.governance.policy import PolicySnapshot
policy = PolicySnapshot.mainline(
    version="frozen-a",
    budgets={"variant_limit": 8, "compile_concurrency": 1},
    observe_shadow_compile=True,
    rollout_authorized=True,
)
print(json.dumps({"document": policy.document, "sha256": policy.sha256}, sort_keys=True))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=worker_environment(),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        worker = json.loads(completed.stdout)
        self.assertEqual(worker["sha256"], policy.sha256)
        self.assertEqual(worker["document"], policy.document)

        mismatch = resolve_mode(
            locally_disabled=False,
            plugin_enabled=True,
            requested_mode="auto",
            compatible=False,
            policy=policy,
        )
        self.assertEqual(mismatch.mode, RuntimeMode.OFF)
        self.assertEqual(mismatch.reason, "incompatible")
