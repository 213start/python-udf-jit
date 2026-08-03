from __future__ import annotations

import json
import unittest

from python_udf_jit.compiler.typed_analysis import (
    BehaviorFamily,
    WorkDimension,
    analyze_typed_module,
)
from tests.unit.compiler.test_typed_ir_v2 import (
    _integer_sum_module,
    _unicode_count_module,
)


class TypedAnalysisTests(unittest.TestCase):
    def test_text_and_numeric_loops_share_generic_patterns(self) -> None:
        text = analyze_typed_module(_unicode_count_module())
        numeric = analyze_typed_module(_integer_sum_module())

        for analysis in (text, numeric):
            self.assertEqual(analysis.behavior.family, BehaviorFamily.NUMERIC_LOOP)
            self.assertEqual(analysis.behavior.loop_count, 1)
            self.assertEqual(analysis.behavior.backedge_count, 1)
            self.assertGreater(
                analysis.behavior.count(WorkDimension.COMPUTE),
                0,
            )
            self.assertEqual(
                [loop.kind for loop in analysis.patterns.loops],
                ["iterator_loop"],
            )
            self.assertEqual(len(analysis.patterns.reductions), 1)
            self.assertEqual(
                analysis.patterns.reductions[0].operation,
                "binary.add",
            )

        self.assertEqual(
            text.patterns.reductions[0].accumulator,
            "%count",
        )
        self.assertEqual(
            numeric.patterns.reductions[0].accumulator,
            "%total",
        )

    def test_type_evidence_keeps_type_dimension_separate(self) -> None:
        analysis = analyze_typed_module(_unicode_count_module())
        evidence = {
            entry.value_id: entry
            for entry in analysis.type_evidence.entries
        }

        self.assertTrue(evidence["%text"].requires_guard)
        self.assertEqual(evidence["%text"].source, "static_schema")
        self.assertFalse(evidence["%count"].requires_guard)
        self.assertEqual(evidence["%count"].source, "block_argument")
        self.assertEqual(evidence["%character"].type.name, "unicode.scalar")

    def test_analysis_round_trip_is_independent_of_semantic_hash(self) -> None:
        module = _unicode_count_module()
        analysis = analyze_typed_module(module)

        decoded = type(analysis).from_documents(
            analysis.to_documents(),
            module=module,
        )

        self.assertEqual(decoded, analysis)
        self.assertEqual(decoded.module_hash, module.semantic_hash)
        self.assertNotEqual(decoded.analysis_hash, module.semantic_hash)

    def test_analysis_documents_contain_no_business_identity(self) -> None:
        document = analyze_typed_module(_unicode_count_module()).to_documents()
        encoded = json.dumps(document, sort_keys=True).lower()

        for forbidden in (
            "fineweb",
            "data-juicer",
            "dj_alphanumeric_ok",
            "pipeline_text_fineweb",
        ):
            self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()
