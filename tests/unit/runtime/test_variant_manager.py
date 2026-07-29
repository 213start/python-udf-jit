from __future__ import annotations

import os
import unittest

from python_udf_jit.runtime.negative_cache import NegativeCache
from python_udf_jit.runtime.variant import VariantKey, WorkerProcessKey
from python_udf_jit.runtime.variant_manager import (
    ResolveKind,
    VariantManager,
    VariantNamespace,
)


def _process(generation: str = "generation-a") -> WorkerProcessKey:
    return WorkerProcessKey(
        "epoch-a",
        "node-a",
        "worker-a",
        os.getpid(),
        generation,
    )


def _key(process: WorkerProcessKey, marker: str = "1") -> VariantKey:
    return VariantKey(
        process=process,
        artifact_content_sha256="0" * 64,
        semantic_hash=marker * 64,
        schema_fingerprint="2" * 64,
        callable_code_sha256="3" * 64,
        artifact_manifest_sha256="4" * 64,
        experiment_manifest_sha256="5" * 64,
        adapter_abi=1,
        runtime_abi=1,
        scalar_slot_abi=1,
        cpython_cinderx_soabi="cpython-314-aarch64-linux-gnu",
        cpu_features=("asimd",),
    )


class VariantManagerTests(unittest.TestCase):
    def test_publish_hit_and_process_generation_isolation(self) -> None:
        process = _process()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=2,
            max_code_bytes=2,
        )
        try:
            pending = manager.resolve(_key(process), lambda: "compiled")
            manager.drain()
            hit = manager.resolve(_key(process), lambda: "must-not-run")
            mismatch = manager.resolve(
                _key(_process("generation-b")),
                lambda: "must-not-run",
            )
        finally:
            manager.close()

        self.assertEqual(pending.kind, ResolveKind.COMPILE_PENDING)
        self.assertEqual(hit.kind, ResolveKind.HIT)
        self.assertEqual(hit.variant.value, "compiled")
        self.assertEqual(mismatch.kind, ResolveKind.PROCESS_MISMATCH)

    def test_eviction_waits_for_active_reference(self) -> None:
        process = _process()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=1,
            max_code_bytes=1,
        )
        first = _key(process, "1")
        second = _key(process, "2")
        try:
            manager.resolve(first, lambda: "first")
            manager.drain()
            with manager.acquire(first) as active:
                manager.resolve(second, lambda: "second")
                manager.drain()
                self.assertEqual(active, "first")
                self.assertIn(first.sha256, manager.active_keys())
            self.assertEqual(manager.active_keys(), (second.sha256,))
        finally:
            manager.close()

    def test_negative_cache_ttl_is_exact_key_scoped(self) -> None:
        now = 100
        cache = NegativeCache(ttl_ns=10, clock=lambda: now)
        cache.record("key-a", "compile_timeout")

        self.assertEqual(cache.get("key-a").reason_code, "compile_timeout")
        self.assertIsNone(cache.get("key-b"))
        now = 110
        self.assertIsNone(cache.get("key-a"))
