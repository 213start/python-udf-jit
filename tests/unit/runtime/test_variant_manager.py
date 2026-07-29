from __future__ import annotations

import os
import threading
import unittest

from python_udf_jit.runtime.circuit_breaker import CircuitBreaker
from python_udf_jit.runtime.negative_cache import NegativeCache
from python_udf_jit.runtime.process_governor import ProcessVariantGovernor
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
        policy_version="scalar-mainline",
        policy_sha256="6" * 64,
    )


class VariantManagerTests(unittest.TestCase):
    def test_publish_hit_and_process_generation_isolation(self) -> None:
        process = _process()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=2,
            max_code_bytes=2,
            code_size=lambda _value: 1,
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

    def test_active_reference_never_allows_a_transient_budget_overrun(
        self,
    ) -> None:
        process = _process()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=1,
            max_code_bytes=1,
            code_size=lambda _value: 1,
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
                self.assertEqual(manager.budget_state(), (1, 1))
            self.assertEqual(manager.active_keys(), (first.sha256,))
            self.assertEqual(
                manager.resolve(second, lambda: "must-not-run").reason_code,
                "negative_cache",
            )
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

    def test_compile_timeout_does_not_block_an_unrelated_key(self) -> None:
        process = _process()
        release = threading.Event()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-a", "tenant-a"),
            max_variants=2,
            max_code_bytes=2,
            max_compile_workers=1,
            compile_timeout_ns=10_000_000,
            negative_ttl_ns=1_000_000_000,
            circuit_failure_threshold=1,
            code_size=lambda _value: 1,
        )
        first_key = _key(process, "1")
        second_key = _key(process, "2")
        try:
            first = manager.resolve(
                first_key,
                lambda: (release.wait(), "late")[1],
            )
            manager.drain()
            timed_out = manager.resolve(first_key, lambda: "must-not-run")
            second = manager.resolve(second_key, lambda: "second")
            manager.drain()
            hit = manager.resolve(second_key, lambda: "must-not-run")
        finally:
            release.set()
            manager.close()

        self.assertEqual(first.kind, ResolveKind.COMPILE_PENDING)
        self.assertEqual(timed_out.reason_code, "negative_cache")
        self.assertEqual(second.kind, ResolveKind.COMPILE_PENDING)
        self.assertEqual(hit.kind, ResolveKind.HIT)

    def test_failure_state_and_process_budget_are_bounded_and_recoverable(
        self,
    ) -> None:
        now = 100
        cache = NegativeCache(
            ttl_ns=100,
            max_entries=2,
            clock=lambda: now,
        )
        for key in ("key-a", "key-b", "key-c"):
            cache.record(key, "compile_failed")
        self.assertIsNone(cache.get("key-a"))
        self.assertEqual(cache.entry_count(), 2)

        breaker = CircuitBreaker(
            failure_threshold=2,
            reset_timeout_ns=10,
            clock=lambda: now,
        )
        breaker.record_internal_failure("compile_failed")
        breaker.record_internal_failure("compile_failed")
        self.assertTrue(breaker.state().open)
        now += 10
        self.assertTrue(breaker.state().half_open)
        self.assertFalse(breaker.record_success().open)

        governor = ProcessVariantGovernor(
            max_namespaces=2,
            max_variants=2,
            max_code_bytes=2,
        )
        self.assertTrue(
            governor.replace(
                "owner-a",
                digest="variant-a",
                code_bytes=1,
                removals=(),
            )
        )
        self.assertTrue(
            governor.replace(
                "owner-b",
                digest="variant-b",
                code_bytes=1,
                removals=(),
            )
        )
        self.assertFalse(
            governor.replace(
                "owner-c",
                digest="variant-c",
                code_bytes=1,
                removals=(),
            )
        )
        governor.release("owner-a")
        self.assertTrue(
            governor.replace(
                "owner-c",
                digest="variant-c",
                code_bytes=1,
                removals=(),
            )
        )
        self.assertEqual(
            (
                governor.state().namespace_count,
                governor.state().variant_count,
                governor.state().code_bytes,
            ),
            (2, 2, 2),
        )

        closed: list[str] = []
        process = _process()
        manager = VariantManager[str](
            process=process,
            namespace=VariantNamespace("job-b", "tenant-a"),
            max_variants=2,
            max_code_bytes=2,
            code_size=lambda _value: 1,
            closer=closed.append,
        )
        first = _key(process, "1")
        second = _key(process, "2")
        third = _key(process, "3")
        try:
            manager.resolve(first, lambda: "first")
            manager.resolve(second, lambda: "second")
            manager.drain()
            with manager.acquire(first):
                manager.resolve(third, lambda: "third")
                manager.drain()
                self.assertEqual(manager.budget_state(), (2, 2))
                self.assertEqual(
                    set(manager.active_keys()),
                    {first.sha256, third.sha256},
                )
                self.assertEqual(closed, ["second"])
        finally:
            manager.close()
        self.assertEqual(set(closed), {"first", "second", "third"})
