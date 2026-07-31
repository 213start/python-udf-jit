from __future__ import annotations

import hashlib
import json
import unittest

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    LogicalType,
    Nullability,
    SemanticBlock,
    SemanticLiteral,
    SemanticOperation,
    build_semantic_module,
)
from python_udf_jit.compiler.region import form_semantic_region_graph
from python_udf_jit.compiler.source_map import (
    SOURCE_MAP_VERSION,
    SourceMap,
    SourceMapEntry,
    SourcePosition,
)
from python_udf_jit.diagnostics.provenance import (
    PROVENANCE_MAP_VERSION,
    ProvenanceEdge,
    ProvenanceLayer,
    ProvenanceMap,
    ProvenanceNode,
    ProvenanceRelation,
    UpperProvenanceRecorder,
    build_bytecode_artifacts,
    build_semantic_artifacts,
)


def _operation(
    operation_id: str,
    op: str,
    operands: tuple[str, ...],
    result_id: str | None,
    source_offset: int,
) -> SemanticOperation:
    return SemanticOperation(
        operation_id,
        "b0",
        op,
        operands,
        result_id,
        LogicalType.FLOAT64,
        Nullability.NON_NULL,
        EffectKind.PURE,
        False,
        None,
        Determinism.DETERMINISTIC,
        (("index", "0"),) if op == "argument" else (),
        None,
        source_offset,
    )


def _module_and_graph():
    operations = (
        _operation("op0", "argument", (), "%0", 0),
        _operation("op1", "return", ("%0",), None, 2),
    )
    module = build_semantic_module(
        function_id="a" * 64,
        entry_block="b0",
        input_types=(LogicalType.FLOAT64,),
        input_nullability=(Nullability.NON_NULL,),
        output_type=LogicalType.FLOAT64,
        output_nullability=Nullability.NON_NULL,
        blocks=(SemanticBlock("b0", ("op0", "op1")),),
        control_edges=(),
        operations=operations,
        return_operation_id="op1",
    )
    return module, form_semantic_region_graph(module)


def _source_map() -> SourceMap:
    return SourceMap(
        SOURCE_MAP_VERSION,
        (
            SourceMapEntry(0, SourcePosition(10, 10, 4, 5)),
            SourceMapEntry(2, SourcePosition(11, 11, 4, 12)),
        ),
    )


class ProvenanceMapTest(unittest.TestCase):
    def test_upper_chain_round_trips_and_traces_in_both_directions(self):
        module, graph = _module_and_graph()
        recorder = UpperProvenanceRecorder(_source_map(), module, graph)
        provenance = recorder.provenance_map

        restored = ProvenanceMap.from_document(
            json.loads(provenance.canonical_bytes())
        )
        source_id = "source:" + module.function_id + ":10:4:10:5"
        operation_id = (
            f"core:{module.semantic_hash}:op0"
        )
        region_id = (
            f"region:{graph.semantic_hash}:{graph.regions[0].region_id}"
        )

        self.assertEqual(restored, provenance)
        self.assertIn(
            operation_id,
            {node.node_id for node in provenance.trace_downstream(source_id)},
        )
        self.assertIn(
            source_id,
            {node.node_id for node in provenance.trace_upstream(operation_id)},
        )
        self.assertIn(
            region_id,
            {node.node_id for node in provenance.trace_downstream(operation_id)},
        )

    def test_fused_cloned_elided_and_synthetic_relations_are_lossless(self):
        nodes = (
            ProvenanceNode("core:s:op0", ProvenanceLayer.CORE_OPERATION, "op"),
            ProvenanceNode("core:s:op1", ProvenanceLayer.CORE_OPERATION, "op"),
            ProvenanceNode(
                "genbc:g:0",
                ProvenanceLayer.GENERATED_BYTECODE,
                "BINARY_OP",
                bytecode_offset=0,
            ),
            ProvenanceNode(
                "genbc:g:2",
                ProvenanceLayer.GENERATED_BYTECODE,
                "BINARY_OP",
                bytecode_offset=2,
            ),
        )
        edges = (
            ProvenanceEdge(
                "core:s:op0",
                "genbc:g:0",
                ProvenanceRelation.FUSED,
                pass_name="scalar_lowering",
            ),
            ProvenanceEdge(
                "core:s:op1",
                "genbc:g:0",
                ProvenanceRelation.FUSED,
                pass_name="scalar_lowering",
            ),
            ProvenanceEdge(
                "core:s:op0",
                "genbc:g:2",
                ProvenanceRelation.CLONED,
                ordinal=1,
            ),
            ProvenanceEdge(
                "core:s:op1",
                None,
                ProvenanceRelation.ELIDED,
                pass_name="dce",
                reason_code="unused",
            ),
            ProvenanceEdge(
                "genbc:g:0",
                "genbc:g:2",
                ProvenanceRelation.SYNTHETIC,
                reason_code="wrapper",
            ),
        )

        provenance = ProvenanceMap(PROVENANCE_MAP_VERSION, nodes, edges)
        self.assertEqual(
            ProvenanceMap.from_document(provenance.to_document()),
            provenance,
        )
        self.assertTrue(
            any(
                edge.from_node_id == "core:s:op1"
                and edge.to_node_id is None
                and edge.relation is ProvenanceRelation.ELIDED
                for edge in provenance.edges
            )
        )

    def test_strict_validation_rejects_duplicate_dangling_and_bad_fields(self):
        source = ProvenanceNode(
            "source:f:1:0:1:1",
            ProvenanceLayer.SOURCE,
            "range",
            source_position=SourcePosition(1, 1, 0, 1),
        )
        cases = (
            ProvenanceMap(
                PROVENANCE_MAP_VERSION,
                (source, source),
                (),
            ),
            ProvenanceMap(
                PROVENANCE_MAP_VERSION,
                (source,),
                (
                    ProvenanceEdge(
                        source.node_id,
                        "pybc:f:0",
                        ProvenanceRelation.DERIVED,
                    ),
                ),
            ),
            ProvenanceMap(
                PROVENANCE_MAP_VERSION,
                (
                    ProvenanceNode(
                        "genbc:g:-2",
                        ProvenanceLayer.GENERATED_BYTECODE,
                        "LOAD_CONST",
                        bytecode_offset=-2,
                    ),
                ),
                (),
            ),
            ProvenanceMap(
                PROVENANCE_MAP_VERSION,
                (
                    ProvenanceNode(
                        "source:f:2:4:1:0",
                        ProvenanceLayer.SOURCE,
                        "range",
                        source_position=SourcePosition(2, 1, 4, 0),
                    ),
                ),
                (),
            ),
        )

        for provenance in cases:
            with self.subTest(provenance=provenance):
                with self.assertRaises(ValueError):
                    provenance.canonical_bytes()
        with self.assertRaises(ValueError):
            ProvenanceMap.from_document(
                {
                    "format_version": PROVENANCE_MAP_VERSION,
                    "nodes": [
                        {
                            **source.to_document(),
                            "layer": "not-a-layer",
                        }
                    ],
                    "edges": [],
                }
            )
        with self.assertRaises(ValueError):
            ProvenanceMap.from_document(
                {
                    "format_version": PROVENANCE_MAP_VERSION,
                    "nodes": [source.to_document()],
                    "edges": [
                        {
                            "from_node_id": source.node_id,
                            "ordinal": None,
                            "pass_name": None,
                            "reason_code": None,
                            "relation": "not-a-relation",
                            "to_node_id": source.node_id,
                        }
                    ],
                }
            )

    def test_readable_artifacts_have_json_and_sanitized_text_forms(self):
        module, graph = _module_and_graph()
        bytecode = build_bytecode_artifacts(
            _source_map.__code__,
            code_hash=hashlib.sha256(b"source-map").hexdigest(),
        )
        semantic = build_semantic_artifacts(module, graph)

        self.assertTrue(bytecode.json_document["instructions"])
        self.assertIn("offset  opname", bytecode.disassembly)
        self.assertNotIn("_source_map", bytecode.disassembly)
        self.assertEqual(
            semantic.core_json["semantic_hash"],
            module.semantic_hash,
        )
        self.assertIn("op0 argument", semantic.core_text)
        self.assertIn(graph.regions[0].region_id, semantic.regions_text)

    def test_semantic_artifacts_redact_literal_bodies(self):
        secret = "customer-secret-9238"
        operations = (
            SemanticOperation(
                "op0",
                "b0",
                "argument",
                (),
                "%0",
                LogicalType.FLOAT64,
                Nullability.NON_NULL,
                EffectKind.PURE,
                False,
                None,
                Determinism.DETERMINISTIC,
                (("index", "0"),),
                None,
                0,
            ),
            SemanticOperation(
                "op1",
                "b0",
                "constant",
                (),
                "%1",
                LogicalType.STRING,
                Nullability.NON_NULL,
                EffectKind.PURE,
                False,
                None,
                Determinism.DETERMINISTIC,
                (),
                SemanticLiteral.from_value(secret),
                0,
            ),
            SemanticOperation(
                "op2",
                "b0",
                "return",
                ("%1",),
                None,
                LogicalType.STRING,
                Nullability.NON_NULL,
                EffectKind.PURE,
                False,
                None,
                Determinism.DETERMINISTIC,
                (),
                None,
                2,
            ),
        )
        module = build_semantic_module(
            function_id="b" * 64,
            entry_block="b0",
            input_types=(LogicalType.FLOAT64,),
            input_nullability=(Nullability.NON_NULL,),
            output_type=LogicalType.STRING,
            output_nullability=Nullability.NON_NULL,
            blocks=(SemanticBlock("b0", ("op0", "op1", "op2")),),
            control_edges=(),
            operations=operations,
            return_operation_id="op2",
        )
        artifacts = build_semantic_artifacts(
            module,
            form_semantic_region_graph(module),
        )
        encoded = json.dumps(
            {
                "core_json": artifacts.core_json,
                "core_text": artifacts.core_text,
            },
            sort_keys=True,
        )

        self.assertNotIn(secret, encoded)
        literal = artifacts.core_json["operations"][1]["literal"]
        self.assertEqual(literal["kind"], "string")
        self.assertEqual(
            literal["sha256"],
            hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
