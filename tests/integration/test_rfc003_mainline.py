from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from python_udf_jit.compiler.analyses import AnalysisManager
from python_udf_jit.compiler.region import form_semantic_region_graph
from tests.semantic_cases import python_continuation_module


class RFC003IntegrationTests(unittest.TestCase):
    def test_rfc003_integration_contract(self):
        module = python_continuation_module()
        graph = form_semantic_region_graph(module)
        analysis = AnalysisManager(module).summary()
        payload = json.dumps(
            {
                "analysis": analysis.to_document(),
                "graph": graph.to_document(),
                "module": module.to_document(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        script = """
import json
import sys
from python_udf_jit.compiler.core_ir import SemanticCoreModule
from python_udf_jit.compiler.analyses import AnalysisSummary
from python_udf_jit.compiler.reference import reference_execute_semantic
from python_udf_jit.compiler.region import SemanticRegionGraph

document = json.loads(sys.stdin.buffer.read())
module = SemanticCoreModule.from_document(document["module"])
graph = SemanticRegionGraph.from_document(document["graph"], module)
analysis = AnalysisSummary.from_document(
    document["analysis"],
    module_hash=module.semantic_hash,
)
effects = []
def execute_python(region, values):
    effects.append([region.resume_id, values[0]])
    return values[0] + 1
result = reference_execute_semantic(
    module,
    (4,),
    python_region_executor=execute_python,
)
print(json.dumps({
    "effects": effects,
    "analysis_hash": analysis.summary_hash,
    "graph_hash": graph.semantic_hash,
    "module_hash": module.semantic_hash,
    "result": result,
}, sort_keys=True, separators=(",", ":")))
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "src:."

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
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode(),
            )
            document = json.loads(completed.stdout)
            self.assertEqual(document["module_hash"], module.semantic_hash)
            self.assertEqual(document["graph_hash"], graph.semantic_hash)
            self.assertEqual(
                document["analysis_hash"],
                analysis.summary_hash,
            )
            self.assertEqual(document["result"], 10)
            self.assertEqual(
                document["effects"],
                [[module.python_regions[0].resume_id, 4]],
            )


if __name__ == "__main__":
    unittest.main()
