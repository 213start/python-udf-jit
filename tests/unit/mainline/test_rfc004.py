from __future__ import annotations

import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.integration.daft_ray.carrier import (
    ObjectRefArtifactHandle,
    ProductionCarrierState,
)
from python_udf_jit.protocol.admission import (
    ComponentCapabilities,
    admit_driver_worker,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import decode_artifact, encode_artifact
from python_udf_jit.protocol.loader import ArtifactLoader, LoaderNamespace


def affine(value):
    return value * 2.0 + 3.0


class RFC004UnitTests(unittest.TestCase):
    def test_rfc004_unit_contract(self):
        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        encoded = encode_artifact(
            build_artifact(
                compiled.core_module,
                compiled.region_graph,
                captured.fallback_identity,
            )
        )
        objects = {}

        def put(payload):
            reference = ("object-ref", len(objects))
            objects[reference] = payload
            return reference

        carrier = ProductionCarrierState.placeholder(
            "rfc004",
            "a" * 64,
        ).finalize(
            encoded,
            inline_threshold=0,
            publisher=put,
        )
        self.assertIsInstance(carrier.handle, ObjectRefArtifactHandle)
        loader = ArtifactLoader(resolver=objects.__getitem__)
        restored = loader.load(
            carrier.handle,
            LoaderNamespace("job", "tenant", "process-generation"),
        )

        self.assertEqual(restored, decode_artifact(encoded))
        self.assertEqual(
            restored.semantic_core_module.semantic_hash,
            compiled.core_module.semantic_hash,
        )
        self.assertTrue(
            admit_driver_worker(
                ComponentCapabilities.current(),
                ComponentCapabilities.current(),
            ).accepted
        )


if __name__ == "__main__":
    unittest.main()
