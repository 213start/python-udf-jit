from __future__ import annotations

import os
import unittest

from python_udf_jit.runtime.variant import (
    CacheDecision,
    ProcessVariantCache,
    VariantKey,
    WorkerProcessKey,
)


def key(process: WorkerProcessKey, **changes) -> VariantKey:
    values = {
        "process": process,
        "artifact_content_sha256": "0" * 64,
        "semantic_hash": "1" * 64,
        "schema_fingerprint": "2" * 64,
        "callable_code_sha256": "3" * 64,
        "artifact_manifest_sha256": "4" * 64,
        "experiment_manifest_sha256": "5" * 64,
        "adapter_abi": 1,
        "runtime_abi": 1,
        "scalar_slot_abi": 1,
        "cpython_cinderx_soabi": "cpython-314-aarch64-linux-gnu",
        "cpu_features": ("asimd",),
        "policy_version": "scalar-mainline",
        "policy_sha256": "6" * 64,
    }
    values.update(changes)
    return VariantKey(**values)


class ProcessVariantCacheTest(unittest.TestCase):
    def setUp(self):
        self.process = WorkerProcessKey(
            "epoch-a", "node-a", "worker-a", os.getpid(), "generation-a"
        )

    def test_first_use_compiles_once_and_exact_key_then_hits(self):
        cache = ProcessVariantCache[str](self.process)
        compile_count = 0

        def compile_variant():
            nonlocal compile_count
            compile_count += 1
            return "code"

        first = cache.resolve(key(self.process), compile_variant)
        second = cache.resolve(key(self.process), compile_variant)

        self.assertEqual(first.decision, CacheDecision.COMPILE)
        self.assertEqual(second.decision, CacheDecision.HIT)
        self.assertEqual(first.value, "code")
        self.assertEqual(compile_count, 1)

    def test_single_variant_never_reuses_or_compiles_a_different_full_key(self):
        cache = ProcessVariantCache[str](self.process)
        cache.resolve(key(self.process), lambda: "code-a")
        compile_count = 0

        def compile_other():
            nonlocal compile_count
            compile_count += 1
            return "code-b"

        for field, value in (
            ("semantic_hash", "9" * 64),
            ("schema_fingerprint", "8" * 64),
            ("callable_code_sha256", "7" * 64),
            ("cpython_cinderx_soabi", "cpython-314-x86_64-linux-gnu"),
            ("cpu_features", ("sve",)),
        ):
            with self.subTest(field=field):
                result = cache.resolve(key(self.process, **{field: value}), compile_other)
                self.assertEqual(result.decision, CacheDecision.MISMATCH)
                self.assertIsNone(result.value)
        self.assertEqual(compile_count, 0)

    def test_process_generation_is_part_of_identity_and_hash(self):
        other = WorkerProcessKey(
            "epoch-a", "node-a", "worker-a", os.getpid(), "generation-b"
        )
        cache = ProcessVariantCache[str](self.process)

        result = cache.resolve(key(other), lambda: "must-not-compile")

        self.assertEqual(result.decision, CacheDecision.MISMATCH)
        self.assertNotEqual(key(self.process).sha256, key(other).sha256)


if __name__ == "__main__":
    unittest.main()
