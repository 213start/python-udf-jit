from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest


class DaftScalarPhysicalizationIntegrationTest(unittest.TestCase):
    def test_five_scalar_layouts_materialize_in_a_fresh_worker_process(
        self,
    ):
        script = r"""
import json
from python_udf_jit.runtime.descriptors import scalar_input_spec, scalar_output_spec
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer

values = {
    "bool": True,
    "int32": -(1 << 31),
    "int64": (1 << 63) - 1,
    "float32": 1.25,
    "float64": -0.0,
}
physicalizer = ScalarPhysicalizer(epoch="integration")
report = {}
for scalar_type, value in values.items():
    with physicalizer.open_call(
        scalar_input_spec(scalar_type, nullable=True),
        scalar_output_spec(scalar_type, nullable=True),
        value,
    ) as frame:
        loaded = frame.load_input()
        frame.stage_output(loaded)
        published = frame.publish_output()
        report[scalar_type] = {
            "input_layout": frame.descriptor_set.input_descriptor.layout_kind,
            "output_layout": frame.descriptor_set.output_descriptor.layout_kind,
            "published": published,
        }
physicalizer.close()
print(json.dumps(report, sort_keys=True))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.getcwd(),
            env=dict(os.environ, PYTHONPATH="src:."),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            set(report),
            {"bool", "int32", "int64", "float32", "float64"},
        )
        self.assertTrue(
            all(
                item["input_layout"] == "scalar_slot"
                and item["output_layout"] == "scalar_slot"
                for item in report.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
