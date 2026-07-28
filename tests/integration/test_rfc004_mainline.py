from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact


def affine(value):
    return value * 2.0 + 3.0


class RFC004IntegrationTests(unittest.TestCase):
    def test_rfc004_integration_contract(self):
        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        encoded = encode_artifact(
            build_artifact(
                compiled.core_module,
                compiled.region_graph,
                captured.fallback_identity,
            )
        )
        script = """
import base64
import json
import os
from python_udf_jit.compiler.reference import reference_execute_semantic
from python_udf_jit.protocol.codec import decode_artifact

payload = base64.b64decode(os.environ["UDFJIT_ARTIFACT"])
artifact = decode_artifact(payload)
print(json.dumps({
    "format_major": artifact.manifest.artifact_format_major,
    "hash": artifact.content_sha256,
    "result": reference_execute_semantic(artifact.semantic_core_module, (4.0,)),
}, sort_keys=True))
"""
        environment = dict(
            os.environ,
            PYTHONPATH="src:.",
            UDFJIT_ARTIFACT=base64.b64encode(encoded).decode("ascii"),
        )

        reports = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=os.getcwd(),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reports.append(json.loads(completed.stdout))

        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]["format_major"], 1)
        self.assertEqual(reports[0]["result"], 11.0)


if __name__ == "__main__":
    unittest.main()
