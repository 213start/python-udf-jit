from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.integration.daft_ray.carrier import (
    ObjectRefArtifactHandle,
    ProductionCarrierState,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import decode_artifact, encode_artifact
from python_udf_jit.protocol.loader import (
    ArtifactLoadError,
    ArtifactLoader,
    ArtifactLoadRejectCode,
    LoaderNamespace,
    clear_prefetched_artifact_payloads,
    prefetch_artifact_payload,
)
from python_udf_jit.protocol.manifest import (
    DEFAULT_MANIFEST,
    DependencyRequirement,
)


def affine(value):
    return value * 2.0 + 3.0


def _encoded(*, dependency_requirements=()):
    captured = capture(CaptureRequest(affine))
    compiled = compile_semantic(captured)
    manifest = dataclasses.replace(
        DEFAULT_MANIFEST,
        dependency_requirements=tuple(dependency_requirements),
    )
    return encode_artifact(
        build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
            manifest,
        )
    )


def _artifact():
    captured = capture(CaptureRequest(affine))
    compiled = compile_semantic(captured)
    return build_artifact(
        compiled.core_module,
        compiled.region_graph,
        captured.fallback_identity,
    )


def _artifact_for_index(base, index):
    manifest = dataclasses.replace(
        DEFAULT_MANIFEST,
        dependency_requirements=(
            DependencyRequirement(
                f"test-dependency-{index:04d}",
                "1.0.0",
            ),
        ),
    )
    return build_artifact(
        base.semantic_core_module,
        base.semantic_region_graph,
        base.fallback_identity,
        manifest,
    )


class _ObjectStore:
    def __init__(self):
        self.values = {}
        self.next_id = 0
        self.get_count = 0

    def put(self, payload: bytes):
        self.next_id += 1
        reference = ("object", self.next_id)
        self.values[reference] = payload
        return reference

    def get(self, reference):
        self.get_count += 1
        return self.values[reference]


class ArtifactLoaderTest(unittest.TestCase):
    def test_sync_actor_thread_rejects_unprefetched_ref_without_ray_get(
        self,
    ):
        encoded = _encoded()
        carrier = ProductionCarrierState.placeholder(
            "candidate",
            "a" * 64,
        ).finalize(
            encoded,
            inline_threshold=0,
            publisher=lambda _payload: "object-ref",
        )
        ray_get = mock.Mock()
        fake_ray = SimpleNamespace(
            get=ray_get,
            get_runtime_context=lambda: SimpleNamespace(
                get_actor_id=lambda: SimpleNamespace(
                    is_nil=lambda: False
                )
            ),
        )

        with mock.patch.dict(sys.modules, {"ray": fake_ray}):
            with self.assertRaises(ArtifactLoadError) as rejected:
                ArtifactLoader().load(
                    carrier.handle,
                    LoaderNamespace(
                        "actor-job",
                        "tenant",
                        "process",
                    ),
                )

        self.assertEqual(
            rejected.exception.code,
            ArtifactLoadRejectCode.OBJECT_MISSING,
        )
        self.assertEqual(
            rejected.exception.detail,
            "RuntimeError",
        )
        ray_get.assert_not_called()

    def test_1_10_100_1000_artifacts_parse_once_per_content(self):
        base = _artifact()
        for count in (1, 10, 100, 1000):
            with self.subTest(count=count):
                decode_count = 0

                def counting_decode(
                    payload,
                    runtime_manifest=DEFAULT_MANIFEST,
                ):
                    nonlocal decode_count
                    decode_count += 1
                    return decode_artifact(payload, runtime_manifest)

                loader = ArtifactLoader(
                    decoder=counting_decode,
                    dependency_resolver=lambda _name: "1.0.0",
                )
                namespace = LoaderNamespace(
                    "job",
                    "tenant",
                    f"process-{count}",
                )
                for index in range(count):
                    encoded = encode_artifact(
                        _artifact_for_index(base, index)
                    )
                    handle = ProductionCarrierState.placeholder(
                        f"candidate-{index}",
                        "a" * 64,
                    ).finalize(
                        encoded,
                        inline_threshold=DEFAULT_MANIFEST.max_total_bytes,
                    ).handle
                    first = loader.load(handle, namespace)
                    self.assertIs(loader.load(handle, namespace), first)

                self.assertEqual(decode_count, count)
                self.assertEqual(loader.positive_entry_count, count)

    def test_large_artifact_uses_object_ref_and_is_cached_per_full_namespace(self):
        encoded = _encoded()
        store = _ObjectStore()
        carrier = ProductionCarrierState.placeholder(
            "candidate", "a" * 64
        ).finalize(
            encoded,
            inline_threshold=0,
            publisher=store.put,
        )
        self.assertIsInstance(carrier.handle, ObjectRefArtifactHandle)

        decode_count = 0

        def counting_decode(payload, runtime_manifest=DEFAULT_MANIFEST):
            nonlocal decode_count
            decode_count += 1
            return decode_artifact(payload, runtime_manifest)

        loader = ArtifactLoader(resolver=store.get, decoder=counting_decode)
        job_a = LoaderNamespace("job-a", "tenant-a", "process-1")
        job_b = LoaderNamespace("job-b", "tenant-a", "process-1")
        tenant_b = LoaderNamespace("job-a", "tenant-b", "process-1")
        restarted = LoaderNamespace("job-a", "tenant-a", "process-2")

        first = loader.load(carrier.handle, job_a)
        self.assertIs(loader.load(carrier.handle, job_a), first)
        loader.load(carrier.handle, job_b)
        loader.load(carrier.handle, tenant_b)
        loader.load(carrier.handle, restarted)

        self.assertEqual(decode_count, 4)
        self.assertEqual(store.get_count, 4)

        async def load_from_actor_event_loop():
            resolver_calls = 0

            def unexpected_resolver(_reference):
                nonlocal resolver_calls
                resolver_calls += 1
                raise AssertionError(
                    "prefetched ObjectRef must not be resolved in the UDF"
                )

            namespace = LoaderNamespace(
                "async-actor-job",
                "tenant-a",
                "process-1",
            )
            prefetch_artifact_payload(
                job_namespace=namespace.job_namespace,
                tenant_namespace=namespace.tenant_namespace,
                content_sha256=carrier.handle.content_sha256,
                payload=encoded,
            )
            try:
                loaded = ArtifactLoader(
                    resolver=unexpected_resolver
                ).load(carrier.handle, namespace)
            finally:
                clear_prefetched_artifact_payloads(
                    job_namespace=namespace.job_namespace,
                    tenant_namespace=namespace.tenant_namespace,
                    content_sha256s=(
                        carrier.handle.content_sha256,
                    ),
                )
            missing_loader = ArtifactLoader()
            with self.assertRaises(
                ArtifactLoadError
            ) as missing:
                missing_loader.load(
                    carrier.handle,
                    LoaderNamespace(
                        "missing-actor-job",
                        "tenant-a",
                        "process-1",
                    ),
                )
            self.assertEqual(
                missing.exception.code,
                ArtifactLoadRejectCode.OBJECT_MISSING,
            )
            return resolver_calls, loaded

        resolver_calls, loaded = asyncio.run(
            load_from_actor_event_loop()
        )
        self.assertEqual(
            loaded.content_sha256,
            carrier.handle.content_sha256,
        )
        self.assertEqual(resolver_calls, 0)

    def test_missing_or_corrupt_object_is_negative_cached_in_its_namespace(self):
        encoded = _encoded()
        store = _ObjectStore()
        carrier = ProductionCarrierState.placeholder(
            "candidate", "a" * 64
        ).finalize(
            encoded,
            inline_threshold=0,
            publisher=store.put,
        )
        store.values[carrier.handle.reference] = b"corrupt"
        loader = ArtifactLoader(resolver=store.get)
        namespace = LoaderNamespace("job-a", "tenant-a", "process-1")

        for _ in range(2):
            with self.assertRaises(ArtifactLoadError):
                loader.load(carrier.handle, namespace)

        self.assertEqual(store.get_count, 1)

    def test_dependency_mismatch_is_rejected_before_semantic_use(self):
        encoded = _encoded(
            dependency_requirements=(
                DependencyRequirement(
                    "python-udf-jit-definitely-missing",
                    "1.0.0",
                ),
            )
        )
        carrier = ProductionCarrierState.placeholder(
            "candidate", "a" * 64
        ).finalize(encoded)
        loader = ArtifactLoader(
            dependency_resolver=lambda _name: None,
        )

        with self.assertRaises(ArtifactLoadError) as raised:
            loader.load(
                carrier.handle,
                LoaderNamespace("job", "tenant", "process"),
            )

        self.assertEqual(
            raised.exception.code,
            ArtifactLoadRejectCode.DEPENDENCY_MISSING,
        )


if __name__ == "__main__":
    unittest.main()
