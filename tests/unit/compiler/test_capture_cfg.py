from __future__ import annotations

import dataclasses
import json
import unittest

from python_udf_jit.compiler.capture_ir import (
    CaptureFrontend,
    build_capture_frontend,
    verify_capture_frontend,
)
from python_udf_jit.compiler.cfg import ControlFlowGraph


def local_join(value):
    result = 1.0
    if value > 0.0:
        result = value
    return result


def short_circuit(value):
    return value > 0.0 and value < 10.0


def exception_path(value):
    try:
        return value + 1.0
    except TypeError:
        return 0.0


def readonly_shapes(value):
    left = (value, 1.0)
    right = [value, 2.0]
    return (left, right)


class CaptureCfgTest(unittest.TestCase):
    def test_local_join_builds_control_edges_and_local_phi(self):
        frontend = build_capture_frontend(local_join.__code__)
        graph = frontend.control_flow_graph

        self.assertEqual(len(graph.blocks), 3)
        self.assertEqual(
            {edge.kind for edge in graph.edges},
            {"branch_false", "branch_true", "fallthrough"},
        )
        join = graph.blocks[-1]
        local_parameters = [
            parameter
            for parameter in join.parameters
            if parameter.kind == "local"
        ]
        self.assertEqual(len(local_parameters), 1)
        self.assertEqual(local_parameters[0].slot, 1)
        self.assertEqual(len(local_parameters[0].incoming), 2)

    def test_short_circuit_builds_stack_phi(self):
        graph = build_capture_frontend(
            short_circuit.__code__
        ).control_flow_graph

        stack_parameters = [
            parameter
            for block in graph.blocks
            for parameter in block.parameters
            if parameter.kind == "stack"
        ]
        self.assertEqual(len(stack_parameters), 1)
        self.assertEqual(stack_parameters[0].slot, 0)
        self.assertEqual(len(stack_parameters[0].incoming), 2)

    def test_exception_table_builds_explicit_exception_edges(self):
        frontend = build_capture_frontend(exception_path.__code__)
        graph = frontend.control_flow_graph
        exception_edges = [
            edge for edge in graph.edges if edge.kind == "exception"
        ]

        self.assertGreaterEqual(len(exception_edges), 1)
        self.assertIn("exception_flow", frontend.required_capabilities)
        for edge in exception_edges:
            self.assertIsNotNone(edge.handler_index)
            self.assertGreaterEqual(edge.target_stack_depth, 1)

    def test_python_region_capabilities_are_explicit_and_not_jit_types(self):
        frontend = build_capture_frontend(readonly_shapes.__code__)

        self.assertIn("python_region", frontend.required_capabilities)
        self.assertIn("readonly_list", frontend.required_capabilities)
        self.assertIn("readonly_tuple", frontend.required_capabilities)
        self.assertNotIn("vector", frontend.required_capabilities)
        self.assertNotIn("arrow", frontend.required_capabilities)

    def test_frontend_and_cfg_round_trip_deterministically(self):
        frontend = build_capture_frontend(local_join.__code__)
        encoded = frontend.canonical_bytes()
        restored = CaptureFrontend.from_document(json.loads(encoded))

        self.assertEqual(restored, frontend)
        graph_encoded = frontend.control_flow_graph.canonical_bytes(
            frontend.decoded_bytecode
        )
        graph = ControlFlowGraph.from_document(json.loads(graph_encoded))
        self.assertEqual(graph, frontend.control_flow_graph)

    def test_verifier_rejects_each_structural_layer(self):
        frontend = build_capture_frontend(local_join.__code__)
        graph = frontend.control_flow_graph
        corruptions = (
            dataclasses.replace(graph, entry_block="b9999"),
            dataclasses.replace(
                graph,
                blocks=(
                    dataclasses.replace(graph.blocks[0], start_offset=2),
                    *graph.blocks[1:],
                ),
            ),
            dataclasses.replace(
                graph,
                edges=(
                    dataclasses.replace(graph.edges[0], target_stack_depth=99),
                    *graph.edges[1:],
                ),
            ),
            dataclasses.replace(
                graph,
                instruction_states=graph.instruction_states[:-1],
            ),
        )

        for corrupted_graph in corruptions:
            with self.subTest(corrupted_graph=corrupted_graph):
                corrupted = dataclasses.replace(
                    frontend,
                    control_flow_graph=corrupted_graph,
                )
                with self.assertRaises(ValueError):
                    verify_capture_frontend(corrupted)

    def test_cfg_verifier_rejects_join_stack_depth_corruption(self):
        frontend = build_capture_frontend(short_circuit.__code__)
        graph = frontend.control_flow_graph
        join = graph.blocks[-1]
        corrupted = dataclasses.replace(
            frontend,
            control_flow_graph=dataclasses.replace(
                graph,
                blocks=(
                    *graph.blocks[:-1],
                    dataclasses.replace(
                        join,
                        entry_stack_depth=join.entry_stack_depth + 1,
                    ),
                ),
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "CFG does not match decoded bytecode",
        ):
            verify_capture_frontend(corrupted)


if __name__ == "__main__":
    unittest.main()
