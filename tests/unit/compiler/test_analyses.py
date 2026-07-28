from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.compiler.analyses import (
    AnalysisKind,
    AnalysisManager,
    AnalysisSummary,
    StaleAnalysisError,
)
from python_udf_jit.compiler.core_ir import (
    EffectKind,
    rehash_semantic_module,
)
from python_udf_jit.compiler.passes import (
    PassManager,
    PassManagerError,
    PassRejectCode,
)
from python_udf_jit.compiler.pipeline import (
    PassPolicy,
    SemanticCompileStatus,
    compile_semantic,
)
from tests.semantic_cases import (
    affine_semantic_module,
    multitype_semantic_module,
)


class _ChangeEffectPass:
    name = "change_effect"
    required_analyses = frozenset({AnalysisKind.EFFECT})

    def __init__(
        self,
        preserved_analyses: frozenset[AnalysisKind],
    ) -> None:
        self.preserved_analyses = preserved_analyses

    def run(self, module, analyses):
        analyses.require(AnalysisKind.EFFECT)
        operations = list(module.operations)
        operations[2] = dataclasses.replace(
            operations[2],
            effect=EffectKind.READ_GLOBAL,
        )
        return rehash_semantic_module(
            dataclasses.replace(module, operations=tuple(operations))
        )


class SemanticAnalysisTests(unittest.TestCase):
    def test_all_required_analyses_are_value_free_and_hash_bound(self):
        module = multitype_semantic_module()
        summary = AnalysisManager(module).summary()

        self.assertEqual(summary.module_hash, module.semantic_hash)
        self.assertEqual(
            {record.kind for record in summary.records},
            set(AnalysisKind),
        )
        self.assertEqual(
            summary.summary_hash,
            summary.recompute_summary_hash(),
        )
        self.assertEqual(
            AnalysisSummary.from_document(
                summary.to_document(),
                module_hash=module.semantic_hash,
            ),
            summary,
        )
        document = summary.to_document()
        self.assertNotIn("hello", str(document))
        self.assertNotIn("payload", str(document))
        exception = next(
            record
            for record in summary.records
            if record.kind is AnalysisKind.EXCEPTION_ORDER
        )
        self.assertEqual(
            [int(values[0]) for _, values in exception.entries],
            list(range(6)),
        )

    def test_cached_analysis_is_reused_and_unpreserved_proof_recomputes(self):
        module = affine_semantic_module()
        manager = PassManager(
            module,
            max_nodes=64,
            max_iterations=4,
            max_time_ms=1_000,
        )
        manager.analyses.require(AnalysisKind.EFFECT)
        manager.analyses.require(AnalysisKind.EFFECT)
        self.assertEqual(
            manager.analyses.compute_count(AnalysisKind.EFFECT),
            1,
        )

        changed = manager.run(
            (
                _ChangeEffectPass(
                    frozenset({AnalysisKind.TYPE})
                ),
            )
        )

        self.assertEqual(
            changed.operations[2].effect,
            EffectKind.READ_GLOBAL,
        )
        manager.analyses.require(AnalysisKind.EFFECT)
        self.assertEqual(
            manager.analyses.compute_count(AnalysisKind.EFFECT),
            2,
        )

    def test_incorrect_preserved_analysis_is_rejected_as_stale(self):
        manager = PassManager(
            affine_semantic_module(),
            max_nodes=64,
            max_iterations=4,
            max_time_ms=1_000,
        )

        with self.assertRaises(StaleAnalysisError) as raised:
            manager.run(
                (
                    _ChangeEffectPass(
                        frozenset({AnalysisKind.EFFECT})
                    ),
                )
            )

        self.assertEqual(raised.exception.kind, AnalysisKind.EFFECT)

    def test_node_iteration_and_time_budgets_return_no_partial_regions(self):
        rejected = compile_semantic(
            affine_semantic_module(),
            PassPolicy(max_nodes=1),
        )
        self.assertEqual(
            rejected.status,
            SemanticCompileStatus.REJECTED,
        )
        self.assertEqual(rejected.reason_code, "budget_exceeded")
        self.assertIsNone(rejected.core_module)
        self.assertIsNone(rejected.region_graph)

        clock_values = iter((0, 2_000_000))
        manager = PassManager(
            affine_semantic_module(),
            max_nodes=64,
            max_iterations=1,
            max_time_ms=1,
            clock_ns=lambda: next(clock_values),
        )
        with self.assertRaises(PassManagerError) as raised:
            manager.run((_ChangeEffectPass(frozenset()),))
        self.assertEqual(
            raised.exception.code,
            PassRejectCode.BUDGET_EXCEEDED,
        )


if __name__ == "__main__":
    unittest.main()
