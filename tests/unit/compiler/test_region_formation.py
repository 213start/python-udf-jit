from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.compiler.core_ir import (
    EffectKind,
    rehash_semantic_module,
)
from python_udf_jit.compiler.reference import reference_execute_semantic
from python_udf_jit.compiler.region import (
    RegionBoundaryReason,
    RegionEdgeKind,
    SemanticRegionGraph,
    form_semantic_region_graph,
    verify_semantic_region_graph,
)
from tests.semantic_cases import (
    affine_semantic_module,
    branch_semantic_module,
    multitype_semantic_module,
    python_continuation_module,
)


class SemanticRegionFormationTests(unittest.TestCase):
    def test_pure_f64_chain_forms_one_verified_cinderx_candidate(self):
        module = affine_semantic_module()
        graph = form_semantic_region_graph(module)

        self.assertEqual(len(graph.regions), 1)
        self.assertEqual(
            graph.regions[0].provider_candidates,
            ("scalar_cinderx",),
        )
        self.assertEqual(
            graph.regions[0].boundary_before,
            RegionBoundaryReason.FUNCTION_ENTRY,
        )
        self.assertEqual(
            graph.regions[0].boundary_after,
            RegionBoundaryReason.FUNCTION_EXIT,
        )
        restored = SemanticRegionGraph.from_document(
            graph.to_document(),
            module,
        )
        self.assertEqual(restored, graph)
        with self.assertRaisesRegex(
            ValueError,
            "hash mismatch",
        ):
            verify_semantic_region_graph(
                module,
                dataclasses.replace(graph, semantic_hash="0" * 64),
            )

    def test_may_raise_operations_preserve_exception_order_edges(self):
        graph = form_semantic_region_graph(multitype_semantic_module())
        exception_edges = [
            edge
            for edge in graph.edges
            if edge.kind is RegionEdgeKind.EXCEPTION_ORDER
        ]

        self.assertEqual(len(exception_edges), 5)
        self.assertTrue(
            all(
                len(region.operation_ids) == 1
                for region in graph.regions
                if region.boundary_after
                is RegionBoundaryReason.EXCEPTION_BARRIER
                and region.operation_ids[0] in {
                    "op1",
                    "op2",
                    "op3",
                    "op4",
                    "op5",
                    "op8",
                }
            )
        )
        self.assertTrue(
            all(
                "scalar_cinderx" not in region.provider_candidates
                for region in graph.regions
            )
        )

    def test_python_region_is_an_exact_once_continuation_barrier(self):
        module = python_continuation_module()
        graph = form_semantic_region_graph(module)
        effects = []

        def execute_python(region, values):
            effects.append((region.resume_id, values[0]))
            return values[0] + 1

        result = reference_execute_semantic(
            module,
            (4,),
            python_region_executor=execute_python,
        )

        self.assertEqual(result, 10)
        self.assertEqual(len(effects), 1)
        self.assertEqual(
            [region.operation_ids for region in graph.regions],
            [("op0",), ("op1",), ("op2", "op3", "op4")],
        )
        self.assertEqual(
            {
                edge.value_id
                for edge in graph.edges
                if edge.kind is RegionEdgeKind.DATA
            },
            {"%0", "%1"},
        )

    def test_control_flow_and_effect_boundaries_never_merge(self):
        module = branch_semantic_module()
        graph = form_semantic_region_graph(module)

        self.assertEqual(
            reference_execute_semantic(module, (True, -4)),
            -4,
        )
        self.assertEqual(
            reference_execute_semantic(module, (False, -4)),
            -4,
        )
        self.assertEqual(
            sum(
                edge.kind is RegionEdgeKind.CONTROL
                for edge in graph.edges
            ),
            4,
        )
        effect_region = next(
            region
            for region in graph.regions
            if region.operation_ids == ("op3",)
        )
        self.assertEqual(
            effect_region.boundary_after,
            RegionBoundaryReason.EFFECT_BARRIER,
        )

    def test_multiple_effects_receive_explicit_order_edges(self):
        module = multitype_semantic_module()
        operations = list(module.operations)
        operations[1] = dataclasses.replace(
            operations[1],
            effect=EffectKind.READ_GLOBAL,
        )
        operations[2] = dataclasses.replace(
            operations[2],
            effect=EffectKind.READ_GLOBAL,
        )
        changed = rehash_semantic_module(
            dataclasses.replace(module, operations=tuple(operations))
        )

        graph = form_semantic_region_graph(changed)
        effect_edges = [
            edge
            for edge in graph.edges
            if edge.kind is RegionEdgeKind.EFFECT
        ]

        self.assertEqual(len(effect_edges), 1)
        self.assertEqual(
            (effect_edges[0].source_region, effect_edges[0].target_region),
            ("region:1", "region:2"),
        )


if __name__ == "__main__":
    unittest.main()
