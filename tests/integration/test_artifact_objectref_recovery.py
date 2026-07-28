from __future__ import annotations

import pickle
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.integration.daft_ray.carrier import (
    ObjectRefArtifactHandle,
    ProductionCarrierState,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact
from python_udf_jit.protocol.loader import (
    ArtifactLoadError,
    ArtifactLoader,
    LoaderNamespace,
)


def affine(value):
    return value * 2.0 + 3.0


def _encoded_artifact() -> bytes:
    captured = capture(CaptureRequest(affine))
    compiled = compile_semantic(captured)
    return encode_artifact(
        build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
    )


class _ObjectStore:
    def __init__(self):
        self.payloads = {}
        self.reads = 0

    def put(self, payload: bytes):
        reference = ("ray-object-ref", len(self.payloads))
        self.payloads[reference] = payload
        return reference

    def get(self, reference):
        self.reads += 1
        return self.payloads[reference]


class ArtifactObjectRefRecoveryTest(unittest.TestCase):
    def test_object_ref_survives_framework_pickle_and_rebuilds_after_restart(self):
        store = _ObjectStore()
        carrier = ProductionCarrierState.placeholder(
            "candidate-object-ref",
            "a" * 64,
        ).finalize(
            _encoded_artifact(),
            inline_threshold=0,
            publisher=store.put,
        )
        restored = pickle.loads(pickle.dumps(carrier))
        self.assertIsInstance(restored.handle, ObjectRefArtifactHandle)

        loader = ArtifactLoader(resolver=store.get)
        before_restart = LoaderNamespace("job", "tenant", "generation-1")
        after_restart = LoaderNamespace("job", "tenant", "generation-2")
        first = loader.load(restored.handle, before_restart)
        self.assertIs(loader.load(restored.handle, before_restart), first)
        rebuilt = loader.load(restored.handle, after_restart)

        self.assertEqual(first, rebuilt)
        self.assertIsNot(first, rebuilt)
        self.assertEqual(store.reads, 2)

    def test_lost_object_is_negative_cached_without_cross_job_reuse(self):
        store = _ObjectStore()
        carrier = ProductionCarrierState.placeholder(
            "candidate-object-ref",
            "a" * 64,
        ).finalize(
            _encoded_artifact(),
            inline_threshold=0,
            publisher=store.put,
        )
        del store.payloads[carrier.handle.reference]
        loader = ArtifactLoader(resolver=store.get)

        for namespace in (
            LoaderNamespace("job-a", "tenant", "generation"),
            LoaderNamespace("job-a", "tenant", "generation"),
            LoaderNamespace("job-b", "tenant", "generation"),
        ):
            with self.assertRaises(ArtifactLoadError):
                loader.load(carrier.handle, namespace)

        self.assertEqual(store.reads, 2)
        self.assertEqual(loader.negative_entry_count, 2)


if __name__ == "__main__":
    unittest.main()
