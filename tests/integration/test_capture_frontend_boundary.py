from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest

from python_udf_jit.compiler.abstract_interpreter import analyze_function


def _rich_control_flow(value):
    result = (value, "input")
    try:
        if value > 0.0 and value < 10.0:
            result = [value, "bounded"]
    except TypeError:
        result = [0.0, "type-error"]
    return result


class CaptureFrontendBoundaryTest(unittest.TestCase):
    def test_driver_frontend_round_trips_in_fresh_worker_process(self):
        program = analyze_function(_rich_control_flow)
        payload = program.canonical_bytes()
        expected_hash = hashlib.sha256(payload).hexdigest()
        script = """
import hashlib
import json
import sys
from python_udf_jit.compiler.abstract_interpreter import CapturedProgram

payload = sys.stdin.buffer.read()
program = CapturedProgram.from_document(json.loads(payload))
print(json.dumps({
    "capabilities": list(program.frontend.required_capabilities),
    "hash": hashlib.sha256(program.canonical_bytes()).hexdigest(),
    "python_regions": len(program.analysis.python_regions),
    "python_region_instructions": sum(
        instruction.capability == "python_region"
        for instruction in program.frontend.decoded_bytecode.instructions
    ),
}, sort_keys=True))
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "src"

        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-c", script],
                input=payload,
                cwd=os.getcwd(),
                env=environment,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            document = json.loads(completed.stdout)
            self.assertEqual(document["hash"], expected_hash)
            self.assertIn("exception_flow", document["capabilities"])
            self.assertIn("python_region", document["capabilities"])
            self.assertIn("readonly_list", document["capabilities"])
            self.assertIn("readonly_tuple", document["capabilities"])
            self.assertGreater(document["python_regions"], 0)
            self.assertGreater(document["python_region_instructions"], 0)


if __name__ == "__main__":
    unittest.main()
