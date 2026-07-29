from __future__ import annotations

import os
import threading
import unittest

from python_udf_jit.runtime.variant import VariantKey, WorkerProcessKey
from python_udf_jit.runtime.variant_manager import (
    ResolveKind,
    VariantManager,
    VariantNamespace,
)


def _key(process: WorkerProcessKey, schema: str) -> VariantKey:
    return VariantKey(
        process=process,
        artifact_content_sha256="0" * 64,
        semantic_hash="1" * 64,
        schema_fingerprint=schema * 64,
        callable_code_sha256="3" * 64,
        artifact_manifest_sha256="4" * 64,
        experiment_manifest_sha256="5" * 64,
        adapter_abi=1,
        runtime_abi=1,
        scalar_slot_abi=1,
        cpython_cinderx_soabi="cpython-314-aarch64-linux-gnu",
        cpu_features=("asimd",),
        policy_version="policy-a",
        policy_sha256="6" * 64,
    )


class RFC007UnitTests(unittest.TestCase):
    def test_rfc007_unit_contract(self) -> None:
        process = WorkerProcessKey(
            "epoch-rfc007",
            "node-a",
            "worker-a",
            os.getpid(),
            "generation-a",
        )
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=2,
            max_code_bytes=2,
            max_compile_workers=1,
            max_pending_compiles=0,
            circuit_failure_threshold=2,
            code_size=lambda _value: 1,
        )
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def compile_once() -> str:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait()
            return "variant-a"

        decisions = []
        try:
            first = manager.resolve(_key(process, "1"), compile_once)
            entered.wait()
            threads = [
                threading.Thread(
                    target=lambda: decisions.append(
                        manager.resolve(_key(process, "1"), compile_once)
                    )
                )
                for _ in range(99)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            release.set()
            manager.drain()
            hit = manager.resolve(
                _key(process, "1"),
                lambda: "must-not-compile",
            )
            other = manager.resolve(
                _key(process, "2"),
                lambda: "variant-b",
            )
            manager.drain()
        finally:
            manager.close()

        self.assertEqual(first.kind, ResolveKind.COMPILE_PENDING)
        self.assertEqual(
            {decision.reason_code for decision in decisions},
            {"compile_inflight"},
        )
        self.assertEqual(calls, 1)
        self.assertEqual(hit.kind, ResolveKind.HIT)
        self.assertEqual(hit.variant.value, "variant-a")
        self.assertEqual(other.kind, ResolveKind.COMPILE_PENDING)
        self.assertNotEqual(
            _key(process, "1").sha256,
            _key(process, "2").sha256,
        )
