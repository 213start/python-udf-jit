"""Versioned provenance for the diagnostic compiler upper chain.

This module is intentionally not imported by :mod:`python_udf_jit.diagnostics`
or the scalar compiler.  Normal execution therefore does not load or build
diagnostic provenance.
"""
from __future__ import annotations

import ast
import copy
import dis
import hashlib
import json
import marshal
from dataclasses import dataclass
from enum import StrEnum
from types import CodeType
from typing import TYPE_CHECKING, Any, Literal

from python_udf_jit.compiler.core_ir import SemanticCoreModule
from python_udf_jit.compiler.region import (
    SemanticRegionGraph,
    verify_semantic_region_graph,
)
from python_udf_jit.compiler.source_map import (
    SourceMap,
    SourcePosition,
    verify_source_map,
)
from python_udf_jit.compiler.verifier import verify_semantic_module

if TYPE_CHECKING:
    from python_udf_jit.provider.scalar_python.compiler import (
        ScalarLoweringSnapshot,
    )


PROVENANCE_MAP_VERSION = 1
READABLE_ARTIFACT_VERSION = 1
_MAX_NODES = 262_144
_MAX_EDGES = 1_048_576
_MAX_TEXT = 4096


class ProvenanceLayer(StrEnum):
    SOURCE = "source"
    ORIGINAL_BYTECODE = "original_bytecode"
    CORE_OPERATION = "core_operation"
    REGION = "region"
    GENERATED_BYTECODE = "generated_bytecode"


class ProvenanceRelation(StrEnum):
    DERIVED = "derived"
    FUSED = "fused"
    CLONED = "cloned"
    LOWERED = "lowered"
    ELIDED = "elided"
    SYNTHETIC = "synthetic"


_LAYER_PREFIX = {
    ProvenanceLayer.SOURCE: "source:",
    ProvenanceLayer.ORIGINAL_BYTECODE: "pybc:",
    ProvenanceLayer.CORE_OPERATION: "core:",
    ProvenanceLayer.REGION: "region:",
    ProvenanceLayer.GENERATED_BYTECODE: "genbc:",
}


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _require_text(value: object, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_TEXT
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"invalid provenance {name}")


def _position_key(position: SourcePosition) -> str:
    fields = (
        position.line,
        position.column,
        position.end_line,
        position.end_column,
    )
    return ":".join("-" if value is None else str(value) for value in fields)


def _verify_source_position(position: SourcePosition) -> None:
    restored = SourcePosition.from_document(position.to_document())
    if restored.line is None or restored.end_line is None:
        raise ValueError("source provenance requires a line range")
    if restored.line < 1 or restored.end_line < 1:
        raise ValueError("invalid provenance source range")


@dataclass(frozen=True)
class ProvenanceNode:
    node_id: str
    layer: ProvenanceLayer
    kind: str
    source_position: SourcePosition | None = None
    bytecode_offset: int | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "attributes": [list(item) for item in self.attributes],
            "bytecode_offset": self.bytecode_offset,
            "kind": self.kind,
            "layer": self.layer.value,
            "node_id": self.node_id,
            "source_position": (
                None
                if self.source_position is None
                else self.source_position.to_document()
            ),
        }

    @classmethod
    def from_document(cls, document: object) -> "ProvenanceNode":
        expected = {
            "attributes",
            "bytecode_offset",
            "kind",
            "layer",
            "node_id",
            "source_position",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid provenance node fields")
        attributes = document["attributes"]
        if not isinstance(attributes, list) or any(
            not isinstance(item, list) or len(item) != 2
            for item in attributes
        ):
            raise ValueError("invalid provenance node attributes")
        raw_position = document["source_position"]
        return cls(
            document["node_id"],  # type: ignore[arg-type]
            ProvenanceLayer(document["layer"]),  # type: ignore[arg-type]
            document["kind"],  # type: ignore[arg-type]
            (
                None
                if raw_position is None
                else SourcePosition.from_document(raw_position)
            ),
            document["bytecode_offset"],  # type: ignore[arg-type]
            tuple((item[0], item[1]) for item in attributes),
        )


@dataclass(frozen=True)
class ProvenanceEdge:
    from_node_id: str
    to_node_id: str | None
    relation: ProvenanceRelation
    pass_name: str | None = None
    reason_code: str | None = None
    ordinal: int | None = None

    def to_document(self) -> dict[str, object]:
        return {
            "from_node_id": self.from_node_id,
            "ordinal": self.ordinal,
            "pass_name": self.pass_name,
            "reason_code": self.reason_code,
            "relation": self.relation.value,
            "to_node_id": self.to_node_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "ProvenanceEdge":
        expected = {
            "from_node_id",
            "ordinal",
            "pass_name",
            "reason_code",
            "relation",
            "to_node_id",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid provenance edge fields")
        return cls(
            document["from_node_id"],  # type: ignore[arg-type]
            document["to_node_id"],  # type: ignore[arg-type]
            ProvenanceRelation(document["relation"]),  # type: ignore[arg-type]
            document["pass_name"],  # type: ignore[arg-type]
            document["reason_code"],  # type: ignore[arg-type]
            document["ordinal"],  # type: ignore[arg-type]
        )


def _edge_sort_key(edge: ProvenanceEdge) -> tuple[object, ...]:
    return (
        str(edge.from_node_id),
        "" if edge.to_node_id is None else str(edge.to_node_id),
        (
            edge.relation.value
            if isinstance(edge.relation, ProvenanceRelation)
            else str(edge.relation)
        ),
        "" if edge.pass_name is None else str(edge.pass_name),
        "" if edge.reason_code is None else str(edge.reason_code),
        -1 if edge.ordinal is None else edge.ordinal,
    )


@dataclass(frozen=True)
class ProvenanceMap:
    format_version: int
    nodes: tuple[ProvenanceNode, ...]
    edges: tuple[ProvenanceEdge, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            tuple(sorted(self.nodes, key=lambda node: str(node.node_id))),
        )
        object.__setattr__(
            self,
            "edges",
            tuple(sorted(self.edges, key=_edge_sort_key)),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "edges": [edge.to_document() for edge in self.edges],
            "format_version": self.format_version,
            "nodes": [node.to_document() for node in self.nodes],
        }

    def canonical_bytes(self) -> bytes:
        verify_provenance_map(self)
        return _canonical_json(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "ProvenanceMap":
        expected = {"edges", "format_version", "nodes"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid provenance map fields")
        if (
            type(document["format_version"]) is not int
            or not isinstance(document["nodes"], list)
            or not isinstance(document["edges"], list)
        ):
            raise ValueError("invalid provenance map scalar")
        result = cls(
            document["format_version"],
            tuple(
                ProvenanceNode.from_document(value)
                for value in document["nodes"]
            ),
            tuple(
                ProvenanceEdge.from_document(value)
                for value in document["edges"]
            ),
        )
        verify_provenance_map(result)
        return result

    def _trace(
        self,
        node_id: str,
        direction: Literal["upstream", "downstream"],
    ) -> tuple[ProvenanceNode, ...]:
        verify_provenance_map(self)
        node_by_id = {node.node_id: node for node in self.nodes}
        if node_id not in node_by_id:
            raise KeyError(node_id)
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.to_node_id is None:
                continue
            source, target = (
                (edge.from_node_id, edge.to_node_id)
                if direction == "downstream"
                else (edge.to_node_id, edge.from_node_id)
            )
            adjacency.setdefault(source, set()).add(target)
        seen = {node_id}
        pending = list(sorted(adjacency.get(node_id, ())))
        result: list[ProvenanceNode] = []
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            result.append(node_by_id[current])
            pending.extend(sorted(adjacency.get(current, ())))
        return tuple(result)

    def trace_upstream(self, node_id: str) -> tuple[ProvenanceNode, ...]:
        return self._trace(node_id, "upstream")

    def trace_downstream(self, node_id: str) -> tuple[ProvenanceNode, ...]:
        return self._trace(node_id, "downstream")


def verify_provenance_map(provenance: ProvenanceMap) -> None:
    if provenance.format_version != PROVENANCE_MAP_VERSION:
        raise ValueError("unsupported provenance map version")
    if len(provenance.nodes) > _MAX_NODES or len(provenance.edges) > _MAX_EDGES:
        raise ValueError("provenance map size limit")
    node_ids: set[str] = set()
    for node in provenance.nodes:
        _require_text(node.node_id, "node id")
        _require_text(node.kind, "node kind")
        if not isinstance(node.layer, ProvenanceLayer):
            raise ValueError("invalid provenance layer")
        if not node.node_id.startswith(_LAYER_PREFIX[node.layer]):
            raise ValueError("provenance node layer/id mismatch")
        if node.node_id in node_ids:
            raise ValueError("duplicate provenance node")
        node_ids.add(node.node_id)
        if node.layer is ProvenanceLayer.SOURCE:
            if node.source_position is None:
                raise ValueError("source provenance has no range")
            _verify_source_position(node.source_position)
        elif node.source_position is not None:
            raise ValueError("non-source provenance has a source range")
        if node.layer in {
            ProvenanceLayer.ORIGINAL_BYTECODE,
            ProvenanceLayer.GENERATED_BYTECODE,
        }:
            if (
                type(node.bytecode_offset) is not int
                or node.bytecode_offset < 0
                or node.bytecode_offset % 2
            ):
                raise ValueError("invalid provenance bytecode offset")
        elif node.bytecode_offset is not None:
            raise ValueError("non-bytecode provenance has an offset")
        for key, value in node.attributes:
            _require_text(key, "attribute key")
            _require_text(value, "attribute value")
        if node.attributes != tuple(sorted(set(node.attributes))):
            raise ValueError("provenance attributes must be unique and sorted")

    edge_documents: set[bytes] = set()
    for edge in provenance.edges:
        _require_text(edge.from_node_id, "edge source")
        _require_text(edge.to_node_id, "edge target", optional=True)
        _require_text(edge.pass_name, "pass name", optional=True)
        _require_text(edge.reason_code, "reason code", optional=True)
        if not isinstance(edge.relation, ProvenanceRelation):
            raise ValueError("invalid provenance relation")
        if edge.from_node_id not in node_ids:
            raise ValueError("dangling provenance edge source")
        if edge.relation is ProvenanceRelation.ELIDED:
            if edge.to_node_id is not None:
                raise ValueError("elided provenance edge must be terminal")
        elif edge.to_node_id is None:
            raise ValueError("non-elided provenance edge has no target")
        if edge.to_node_id is not None and edge.to_node_id not in node_ids:
            raise ValueError("dangling provenance edge target")
        if (
            edge.ordinal is not None
            and (type(edge.ordinal) is not int or edge.ordinal < 0)
        ):
            raise ValueError("invalid provenance edge ordinal")
        encoded = _canonical_json(edge.to_document())
        if encoded in edge_documents:
            raise ValueError("duplicate provenance edge")
        edge_documents.add(encoded)


@dataclass(frozen=True)
class BytecodeArtifacts:
    json_document: dict[str, object]
    disassembly: str


@dataclass(frozen=True)
class SemanticArtifacts:
    core_json: dict[str, object]
    core_text: str
    regions_json: dict[str, object]
    regions_text: str


def _constant_summary(value: object) -> dict[str, str]:
    try:
        encoded = marshal.dumps(value)
    except (TypeError, ValueError):
        encoded = type(value).__qualname__.encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "type": type(value).__qualname__,
    }


def build_bytecode_artifacts(
    code: CodeType,
    *,
    code_hash: str,
) -> BytecodeArtifacts:
    if type(code) is not CodeType:
        raise TypeError("bytecode artifacts require a code object")
    _require_text(code_hash, "code hash")
    instructions: list[dict[str, object]] = []
    lines = ["offset  opname                         arg"]
    for instruction in dis.get_instructions(code, show_caches=True):
        position = instruction.positions
        instructions.append(
            {
                "arg": instruction.arg,
                "is_jump_target": instruction.is_jump_target,
                "offset": instruction.offset,
                "opname": instruction.opname,
                "position": {
                    "column": position.col_offset,
                    "end_column": position.end_col_offset,
                    "end_line": position.end_lineno,
                    "line": position.lineno,
                },
            }
        )
        argument = "" if instruction.arg is None else str(instruction.arg)
        lines.append(
            f"{instruction.offset:>6}  {instruction.opname:<29} {argument}"
        )
    return BytecodeArtifacts(
        {
            "code_hash": code_hash,
            "constants": [
                _constant_summary(value) for value in code.co_consts
            ],
            "format_version": READABLE_ARTIFACT_VERSION,
            "instructions": instructions,
        },
        "\n".join(lines) + "\n",
    )


def build_semantic_artifacts(
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
) -> SemanticArtifacts:
    verify_semantic_module(module)
    verify_semantic_region_graph(module, graph)
    core_lines = [
        f"semantic_hash {module.semantic_hash}",
        f"function_id {module.function_id}",
    ]
    for operation in module.operations:
        literal = (
            ""
            if operation.literal is None
            else (
                " literal="
                + _constant_summary(operation.literal.value)["sha256"]
            )
        )
        core_lines.append(
            f"{operation.operation_id} {operation.op}"
            f" operands={','.join(operation.operands)}"
            f" result={operation.result_id or '-'}"
            f" source_offset={operation.source_offset!s}{literal}"
        )
    region_lines = [
        f"semantic_hash {graph.semantic_hash}",
        f"module_hash {graph.module_hash}",
    ]
    for region in graph.regions:
        region_lines.append(
            f"{region.region_id} block={region.block_id}"
            f" operations={','.join(region.operation_ids)}"
        )
    core_document = module.to_document()
    for operation, operation_document in zip(
        module.operations,
        core_document["operations"],
        strict=True,
    ):
        if operation.literal is None:
            continue
        operation_document["literal"] = {
            "kind": operation.literal.kind.value,
            "sha256": hashlib.sha256(
                operation.literal.encoded_value.encode("utf-8")
            ).hexdigest(),
        }
    return SemanticArtifacts(
        core_document,
        "\n".join(core_lines) + "\n",
        graph.to_document(),
        "\n".join(region_lines) + "\n",
    )


class UpperProvenanceRecorder:
    """Build Source -> bytecode -> Core/Region and accept scalar lowering."""

    def __init__(
        self,
        source_map: SourceMap,
        module: SemanticCoreModule,
        graph: SemanticRegionGraph,
    ) -> None:
        verify_source_map(source_map)
        verify_semantic_module(module)
        verify_semantic_region_graph(module, graph)
        self._source_map = source_map
        self._module = module
        self._graph = graph
        self._nodes: list[ProvenanceNode] = []
        self._edges: list[ProvenanceEdge] = []
        self._generated_ast_text = ""
        self._lowering_map: dict[str, object] = {
            "entries": [],
            "format_version": READABLE_ARTIFACT_VERSION,
        }
        self._generated_bytecode: BytecodeArtifacts | None = None
        self._lowering_recorded = False
        self._semantic_artifacts = build_semantic_artifacts(module, graph)
        self._record_upper_chain()
        verify_provenance_map(self.provenance_map)

    @property
    def provenance_map(self) -> ProvenanceMap:
        result = ProvenanceMap(
            PROVENANCE_MAP_VERSION,
            tuple(self._nodes),
            tuple(self._edges),
        )
        verify_provenance_map(result)
        return result

    @property
    def generated_ast_text(self) -> str:
        return self._generated_ast_text

    @property
    def lowering_map(self) -> dict[str, object]:
        return self._lowering_map

    @property
    def generated_bytecode_artifacts(self) -> BytecodeArtifacts | None:
        return self._generated_bytecode

    @property
    def semantic_artifacts(self) -> SemanticArtifacts:
        return self._semantic_artifacts

    def _record_upper_chain(self) -> None:
        bytecode_nodes: dict[int, str] = {}
        seen_source_nodes: set[str] = set()
        for entry in self._source_map.entries:
            bytecode_id = (
                f"pybc:{self._module.function_id}:{entry.bytecode_offset}"
            )
            bytecode_nodes[entry.bytecode_offset] = bytecode_id
            self._nodes.append(
                ProvenanceNode(
                    bytecode_id,
                    ProvenanceLayer.ORIGINAL_BYTECODE,
                    "instruction",
                    bytecode_offset=entry.bytecode_offset,
                )
            )
            if entry.position.line is None:
                continue
            source_id = (
                f"source:{self._module.function_id}:"
                f"{_position_key(entry.position)}"
            )
            if source_id not in seen_source_nodes:
                seen_source_nodes.add(source_id)
                self._nodes.append(
                    ProvenanceNode(
                        source_id,
                        ProvenanceLayer.SOURCE,
                        "source_range",
                        source_position=entry.position,
                    )
                )
            self._edges.append(
                ProvenanceEdge(
                    source_id,
                    bytecode_id,
                    ProvenanceRelation.DERIVED,
                )
            )

        operation_nodes: dict[str, str] = {}
        for operation in self._module.operations:
            operation_id = (
                f"core:{self._module.semantic_hash}:"
                f"{operation.operation_id}"
            )
            operation_nodes[operation.operation_id] = operation_id
            self._nodes.append(
                ProvenanceNode(
                    operation_id,
                    ProvenanceLayer.CORE_OPERATION,
                    operation.op,
                    attributes=tuple(
                        sorted(
                            (
                                ("block_id", operation.block_id),
                                ("operation_id", operation.operation_id),
                            )
                        )
                    ),
                )
            )
            if operation.source_offset is None:
                continue
            source_bytecode = bytecode_nodes.get(operation.source_offset)
            if source_bytecode is None:
                raise ValueError(
                    "semantic operation references unknown bytecode offset"
                )
            self._edges.append(
                ProvenanceEdge(
                    source_bytecode,
                    operation_id,
                    ProvenanceRelation.LOWERED,
                    pass_name="semantic_lowering",
                )
            )

        for region in self._graph.regions:
            region_id = (
                f"region:{self._graph.semantic_hash}:{region.region_id}"
            )
            self._nodes.append(
                ProvenanceNode(
                    region_id,
                    ProvenanceLayer.REGION,
                    "semantic_region",
                    attributes=tuple(
                        sorted(
                            (
                                ("block_id", region.block_id),
                                ("region_id", region.region_id),
                            )
                        )
                    ),
                )
            )
            for operation_id in region.operation_ids:
                self._edges.append(
                    ProvenanceEdge(
                        operation_nodes[operation_id],
                        region_id,
                        ProvenanceRelation.LOWERED,
                        pass_name="region_formation",
                    )
                )

    def record_scalar_lowering(
        self,
        snapshot: "ScalarLoweringSnapshot",
    ) -> None:
        if self._lowering_recorded:
            raise ValueError("scalar lowering provenance already recorded")
        if (
            snapshot.semantic_hash != self._module.semantic_hash
            or snapshot.region_graph_hash != self._graph.semantic_hash
        ):
            raise ValueError("scalar lowering snapshot identity mismatch")
        if len({line for line, _ in snapshot.operation_lines}) != len(
            snapshot.operation_lines
        ):
            raise ValueError("duplicate scalar lowering origin line")
        known_operations = {
            operation.operation_id for operation in self._module.operations
        }
        if any(
            not origins
            or any(
                operation_id not in known_operations
                for operation_id in origins
            )
            for _, origins in snapshot.operation_lines
        ):
            raise ValueError("invalid scalar lowering operation origin")
        line_origins = dict(snapshot.operation_lines)
        operation_nodes = {
            operation.operation_id: (
                f"core:{self._module.semantic_hash}:"
                f"{operation.operation_id}"
            )
            for operation in self._module.operations
        }
        region_by_operation = {
            operation_id: (
                f"region:{self._graph.semantic_hash}:{region.region_id}"
            )
            for region in self._graph.regions
            for operation_id in region.operation_ids
        }
        entries: list[dict[str, object]] = []
        for instruction in dis.get_instructions(
            snapshot.generated_code,
            show_caches=True,
        ):
            generated_id = (
                f"genbc:{snapshot.generated_code_hash}:"
                f"{instruction.offset}"
            )
            line = instruction.positions.lineno
            origins = () if line is None else line_origins.get(line, ())
            attributes = (
                (("synthetic_kind", "scalar_wrapper"),)
                if not origins
                else (("synthetic_line", str(line)),)
            )
            self._nodes.append(
                ProvenanceNode(
                    generated_id,
                    ProvenanceLayer.GENERATED_BYTECODE,
                    instruction.opname,
                    bytecode_offset=instruction.offset,
                    attributes=attributes,
                )
            )
            for operation_id in origins:
                core_id = operation_nodes[operation_id]
                relation = (
                    ProvenanceRelation.FUSED
                    if len(origins) > 1
                    else ProvenanceRelation.LOWERED
                )
                self._edges.append(
                    ProvenanceEdge(
                        core_id,
                        generated_id,
                        relation,
                        pass_name="scalar_python_lowering",
                    )
                )
                self._edges.append(
                    ProvenanceEdge(
                        region_by_operation[operation_id],
                        generated_id,
                        relation,
                        pass_name="scalar_python_lowering",
                    )
                )
            entries.append(
                {
                    "bytecode_offset": instruction.offset,
                    "operation_ids": list(origins),
                    "synthetic_line": line,
                }
            )
        self._generated_ast_text = snapshot.generated_ast_text
        self._lowering_map = {
            "entries": entries,
            "format_version": READABLE_ARTIFACT_VERSION,
            "generated_code_hash": snapshot.generated_code_hash,
            "region_graph_hash": snapshot.region_graph_hash,
            "semantic_hash": snapshot.semantic_hash,
        }
        self._generated_bytecode = build_bytecode_artifacts(
            snapshot.generated_code,
            code_hash=snapshot.generated_code_hash,
        )
        verify_provenance_map(self.provenance_map)
        self._lowering_recorded = True


def build_upper_provenance(
    source_map: SourceMap,
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
) -> ProvenanceMap:
    """Build the immutable Source/bytecode/Core/Region provenance prefix."""

    return UpperProvenanceRecorder(
        source_map,
        module,
        graph,
    ).provenance_map


def generated_ast_text(module: ast.AST) -> str:
    """Return a deterministic AST without embedding literal bodies."""

    if not isinstance(module, ast.AST):
        raise TypeError("generated AST artifact requires an AST")
    sanitized = copy.deepcopy(module)

    class _RedactConstants(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.Constant:
            summary = _constant_summary(node.value)
            replacement = ast.Constant(
                value=(
                    f"<redacted:{summary['type']}:{summary['sha256']}>"
                )
            )
            return ast.copy_location(replacement, node)

    sanitized = _RedactConstants().visit(sanitized)
    return ast.dump(
        sanitized,
        annotate_fields=True,
        include_attributes=True,
        indent=2,
    )
