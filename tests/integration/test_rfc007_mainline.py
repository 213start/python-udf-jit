from __future__ import annotations

import json
import subprocess
import sys
import unittest

from tests.integration.test_driver_worker_artifact_roundtrip import (
    worker_environment,
)


class RFC007IntegrationTests(unittest.TestCase):
    def test_rfc007_integration_contract(self) -> None:
        script = r"""
import json
import os
from python_udf_jit.runtime.variant import VariantKey, WorkerProcessKey
from python_udf_jit.runtime.variant_manager import VariantManager, VariantNamespace

process = WorkerProcessKey("epoch", "node", "worker", os.getpid(), "fresh")
key = VariantKey(
    process, "0" * 64, "1" * 64, "2" * 64, "3" * 64, "4" * 64,
    "5" * 64, 1, 1, 1, "cpython-314-aarch64-linux-gnu", ("asimd",),
    "scalar-mainline", "6" * 64
)
manager = VariantManager(
    process=process,
    namespace=VariantNamespace("job-a", "tenant-a"),
    max_variants=2,
    max_code_bytes=2,
    code_size=lambda _value: 1,
)
try:
    first = manager.resolve(key, lambda: "fresh-code")
    manager.drain()
    second = manager.resolve(key, lambda: "wrong")
    print(json.dumps({
        "first": first.kind,
        "second": second.kind,
        "value": second.variant.value,
        "pid": os.getpid(),
    }, sort_keys=True))
finally:
    manager.close()
"""
        reports = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=worker_environment(),
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reports.append(json.loads(completed.stdout))

        self.assertEqual(
            {(report["first"], report["second"]) for report in reports},
            {("compile_pending", "hit")},
        )
        self.assertEqual(
            {report["value"] for report in reports},
            {"fresh-code"},
        )
        self.assertEqual(len({report["pid"] for report in reports}), 2)
