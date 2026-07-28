from __future__ import annotations

import unittest

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.analyses import AnalysisKind
from python_udf_jit.compiler.pipeline import (
    SemanticCompileStatus,
    compile_semantic,
    compile_semantic_sequence,
)
from python_udf_jit.compiler.reference import reference_execute_semantic
from tests.semantic_cases import (
    affine_semantic_module,
    multitype_semantic_module,
)


def _controlled_string(value):
    return str(value).upper()


class RFC003UnitTests(unittest.TestCase):
    def test_rfc003_unit_contract(self):
        affine, multitype = compile_semantic_sequence(
            (
                affine_semantic_module(),
                multitype_semantic_module(),
            )
        )

        self.assertEqual(
            (affine.status, multitype.status),
            (
                SemanticCompileStatus.COMPILED,
                SemanticCompileStatus.COMPILED,
            ),
        )
        self.assertNotEqual(
            affine.region_graph.semantic_hash,
            multitype.region_graph.semantic_hash,
        )
        self.assertEqual(
            affine.region_graph.regions[0].region_id,
            multitype.region_graph.regions[0].region_id,
        )
        self.assertEqual(
            {record.kind for record in multitype.analysis_summary.records},
            set(AnalysisKind),
        )
        self.assertEqual(
            reference_execute_semantic(affine.core_module, (4.0,)),
            11.0,
        )

        fallback = compile_semantic(analyze_function(_controlled_string))
        self.assertEqual(
            fallback.status,
            SemanticCompileStatus.PYTHON_FALLBACK,
        )
        self.assertEqual(
            len(fallback.core_module.python_regions),
            1,
        )
        self.assertTrue(
            all(
                not region.provider_candidates
                for region in fallback.region_graph.regions
            )
        )


if __name__ == "__main__":
    unittest.main()
