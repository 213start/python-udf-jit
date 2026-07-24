from __future__ import annotations

import base64
import hashlib
import json
import operator
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper
from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact


def scalar_affine(value):
    return value * 2.0 + 3.0


def side_effect_daft_method(_self, value):
    with Path(os.environ["UDFJIT_SIDE_EFFECT_FILE"]).open("a", encoding="utf-8") as stream:
        stream.write(f"{value!r}\n")
    return scalar_affine(value)


side_effect_daft_method.__wrapped__ = scalar_affine


class DriverWorkerArtifactRoundtripTest(unittest.TestCase):
    def test_fallback_wrapper_survives_an_independent_worker_process(self):
        wrapper = FallbackOnlyWrapper(
            candidate_id="candidate-roundtrip",
            original_callable=operator.mul,
            carrier=ProductionCarrierState.placeholder(
                "candidate-roundtrip", "c" * 64
            ),
        )
        wrapper.finalize("{'left': 'int64', 'right': 'int64'}", "projection")
        payload = base64.b64encode(pickle.dumps(wrapper)).decode("ascii")
        script = """
import base64, json, os, pickle
wrapper = pickle.loads(base64.b64decode(os.environ['UDFJIT_WRAPPER']))
print(json.dumps({
    'candidate_id': wrapper.candidate_id,
    'carrier_hash': wrapper.carrier.state_sha256,
    'result': wrapper(6, 7),
    'usage': wrapper.usage_context,
}))
"""
        env = dict(os.environ, UDFJIT_WRAPPER=payload)

        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        observation = json.loads(completed.stdout)
        self.assertEqual(observation["candidate_id"], "candidate-roundtrip")
        self.assertEqual(observation["carrier_hash"], wrapper.carrier.state_sha256)
        self.assertEqual(observation["result"], 42)
        self.assertEqual(observation["usage"], "projection")

    def test_inline_artifact_bytes_survive_the_wrapper_worker_roundtrip(self):
        def affine(value):
            return value * 2.0 + 3.0

        module = lower_capture(capture(CaptureRequest(affine)))
        encoded = encode_artifact(
            build_artifact(
                module,
                form_verified_region(module),
                module.fallback_identity,
            )
        )
        wrapper = FallbackOnlyWrapper(
            candidate_id="candidate-with-artifact",
            original_callable=operator.add,
            carrier=ProductionCarrierState.placeholder(
                "candidate-with-artifact", "d" * 64
            ).finalize(encoded),
        )
        payload = base64.b64encode(pickle.dumps(wrapper)).decode("ascii")
        script = """
import base64, json, os, pickle
from python_udf_jit.protocol.codec import decode_artifact
wrapper = pickle.loads(base64.b64decode(os.environ['UDFJIT_WRAPPER']))
artifact = decode_artifact(wrapper.carrier.artifact_bytes)
print(json.dumps({
    'content_hash': artifact.content_sha256,
    'semantic_hash': artifact.core_module.semantic_hash,
    'fallback_result': wrapper(19, 23),
}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ, UDFJIT_WRAPPER=payload),
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        observation = json.loads(completed.stdout)
        self.assertEqual(observation["content_hash"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(observation["semantic_hash"], module.semantic_hash)
        self.assertEqual(observation["fallback_result"], 42)

    def test_hash_valid_carrier_with_invalid_artifact_fails_open_once_in_worker_process(self):
        wrapper = FallbackOnlyWrapper(
            candidate_id="candidate-invalid-worker-artifact",
            original_callable=side_effect_daft_method,
            carrier=ProductionCarrierState.placeholder(
                "candidate-invalid-worker-artifact", "e" * 64
            ).finalize(b"hash-valid-but-not-an-artifact"),
        )
        wrapper.finalize("{'value': 'float64'}", "projection")
        payload = base64.b64encode(pickle.dumps(wrapper)).decode("ascii")
        script = """
import base64, json, os, pickle
wrapper = pickle.loads(base64.b64decode(os.environ['UDFJIT_WRAPPER']))
print(json.dumps({'result': wrapper(None, 2.0)}))
"""
        with tempfile.TemporaryDirectory() as directory:
            side_effect_file = Path(directory, "calls.txt")
            env = dict(
                os.environ,
                UDFJIT_WRAPPER=payload,
                UDFJIT_MODE="auto",
                UDFJIT_RUN_ID="run-roundtrip",
                UDFJIT_CLUSTER_EPOCH="epoch-roundtrip",
                UDFJIT_NODE_ID="worker-node",
                UDFJIT_ACTOR_WORKER_ID="worker-process",
                UDFJIT_PROCESS_GENERATION="generation-roundtrip",
                UDFJIT_SIDE_EFFECT_FILE=str(side_effect_file),
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["result"], 7.0)
            self.assertEqual(side_effect_file.read_text(encoding="utf-8").splitlines(), ["2.0"])


if __name__ == "__main__":
    unittest.main()
