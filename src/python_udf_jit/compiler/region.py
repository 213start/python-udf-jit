from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any

from python_udf_jit.compiler.core_ir import (
    EffectKind,
    LogicalType,
    Nullability,
    CoreUdfModule,
    SemanticCoreModule,
)
from python_udf_jit.compiler.verifier import (
    verify_core_module,
    verify_region,
    verify_semantic_module,
)


@dataclass(frozen=True)
class VerifiedRegion:
    format_version: int
    region_id: str
    entry_values: tuple[str, ...]
    exit_values: tuple[str, ...]
    operation_indexes: tuple[int, ...]
    pure: bool
    single_entry: bool
    single_exit: bool
    semantic_hash: str

    def to_document(self) -> dict[str, Any]:
        return {
            "entry_values": list(self.entry_values),
            "exit_values": list(self.exit_values),
            "format_version": self.format_version,
            "operation_indexes": list(self.operation_indexes),
            "pure": self.pure,
            "region_id": self.region_id,
            "semantic_hash": self.semantic_hash,
            "single_entry": self.single_entry,
            "single_exit": self.single_exit,
        }

    @classmethod
    def from_document(cls, document: object) -> "VerifiedRegion":
        expected = {
            "entry_values",
            "exit_values",
            "format_version",
            "operation_indexes",
            "pure",
            "region_id",
            "semantic_hash",
            "single_entry",
            "single_exit",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid region fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid region format version")
        entry = document["entry_values"]
        exits = document["exit_values"]
        indexes = document["operation_indexes"]
        if (
            not isinstance(entry, list)
            or not all(isinstance(value, str) for value in entry)
            or not isinstance(exits, list)
            or not all(isinstance(value, str) for value in exits)
            or not isinstance(indexes, list)
            or not all(type(value) is int for value in indexes)
        ):
            raise ValueError("invalid region sequences")
        if not isinstance(document["region_id"], str) or not isinstance(document["semantic_hash"], str):
            raise ValueError("invalid region strings")
        bool_values = (document["pure"], document["single_entry"], document["single_exit"])
        if not all(type(value) is bool for value in bool_values):
            raise ValueError("invalid region flags")
        return cls(
            document["format_version"],
            document["region_id"],
            tuple(entry),
            tuple(exits),
            tuple(indexes),
            *bool_values,
            document["semantic_hash"],
        )


def form_verified_region(module: CoreUdfModule) -> VerifiedRegion:
    verify_core_module(module)
    region = VerifiedRegion(
        1,
        "scalar:0",
        ("%0",),
        (module.return_value,),
        tuple(range(len(module.nodes))),
        True,
        True,
        True,
        module.semantic_hash,
    )
    verify_region(module, region)
    return region


SEMANTIC_REGION_GRAPH_VERSION = 1


class RegionBoundaryReason(StrEnum):
    FUNCTION_ENTRY = "function_entry"
    FUNCTION_EXIT = "function_exit"
    BLOCK_BOUNDARY = "block_boundary"
    CONTROL_FLOW = "control_flow"
    EFFECT_BARRIER = "effect_barrier"
    EXCEPTION_BARRIER = "exception_barrier"
    PYTHON_REGION = "python_region"


class RegionEdgeKind(StrEnum):
    DATA = "data"
    CONTROL = "control"
    EFFECT = "effect"
    EXCEPTION_ORDER = "exception_order"


@dataclass(frozen=True)
class SemanticRegion:
    region_id: str
    block_id: str
    operation_ids: tuple[str, ...]
    entry_values: tuple[str, ...]
    exit_values: tuple[str, ...]
    provider_candidates: tuple[str, ...]
    boundary_before: RegionBoundaryReason
    boundary_after: RegionBoundaryReason

    def to_document(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "boundary_after": self.boundary_after.value,
            "boundary_before": self.boundary_before.value,
            "entry_values": list(self.entry_values),
            "exit_values": list(self.exit_values),
            "operation_ids": list(self.operation_ids),
            "provider_candidates": list(self.provider_candidates),
            "region_id": self.region_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticRegion":
        expected = {
            "block_id",
            "boundary_after",
            "boundary_before",
            "entry_values",
            "exit_values",
            "operation_ids",
            "provider_candidates",
            "region_id",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic region fields")
        for name in ("block_id", "region_id"):
            if not isinstance(document[name], str):
                raise ValueError("invalid semantic region string")
        for name in (
            "entry_values",
            "exit_values",
            "operation_ids",
            "provider_candidates",
        ):
            if (
                not isinstance(document[name], list)
                or any(
                    not isinstance(value, str)
                    for value in document[name]
                )
            ):
                raise ValueError("invalid semantic region sequence")
        return cls(
            document["region_id"],
            document["block_id"],
            tuple(document["operation_ids"]),
            tuple(document["entry_values"]),
            tuple(document["exit_values"]),
            tuple(document["provider_candidates"]),
            RegionBoundaryReason(document["boundary_before"]),
            RegionBoundaryReason(document["boundary_after"]),
        )


@dataclass(frozen=True)
class SemanticRegionEdge:
    source_region: str
    target_region: str
    kind: RegionEdgeKind
    value_id: str | None = None

    def to_document(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "source_region": self.source_region,
            "target_region": self.target_region,
            "value_id": self.value_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticRegionEdge":
        expected = {
            "kind",
            "source_region",
            "target_region",
            "value_id",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic region edge fields")
        for name in ("source_region", "target_region"):
            if not isinstance(document[name], str):
                raise ValueError("invalid semantic region edge string")
        if (
            document["value_id"] is not None
            and not isinstance(document["value_id"], str)
        ):
            raise ValueError("invalid semantic region edge value")
        return cls(
            document["source_region"],
            document["target_region"],
            RegionEdgeKind(document["kind"]),
            document["value_id"],
        )


@dataclass(frozen=True)
class SemanticRegionGraph:
    format_version: int
    module_hash: str
    regions: tuple[SemanticRegion, ...]
    edges: tuple[SemanticRegionEdge, ...]
    semantic_hash: str

    def semantic_document(self) -> dict[str, Any]:
        return {
            "edges": [edge.to_document() for edge in self.edges],
            "format_version": self.format_version,
            "module_hash": self.module_hash,
            "regions": [region.to_document() for region in self.regions],
        }

    def recompute_semantic_hash(self) -> str:
        encoded = json.dumps(
            self.semantic_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(
            b"python-udf-jit-semantic-region-graph-v1\0" + encoded
        ).hexdigest()

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_document(
        cls,
        document: object,
        module: SemanticCoreModule,
    ) -> "SemanticRegionGraph":
        expected = {
            "edges",
            "format_version",
            "module_hash",
            "regions",
            "semantic_hash",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic region graph fields")
        if (
            type(document["format_version"]) is not int
            or not isinstance(document["module_hash"], str)
            or not isinstance(document["semantic_hash"], str)
            or not isinstance(document["regions"], list)
            or not isinstance(document["edges"], list)
        ):
            raise ValueError("invalid semantic region graph scalar")
        result = cls(
            document["format_version"],
            document["module_hash"],
            tuple(
                SemanticRegion.from_document(value)
                for value in document["regions"]
            ),
            tuple(
                SemanticRegionEdge.from_document(value)
                for value in document["edges"]
            ),
            document["semantic_hash"],
        )
        verify_semantic_region_graph(module, result)
        return result


def _barrier_reason(operation: object) -> RegionBoundaryReason | None:
    if getattr(operation, "op") == "python.region":
        return RegionBoundaryReason.PYTHON_REGION
    if getattr(operation, "effect") is not EffectKind.PURE:
        return RegionBoundaryReason.EFFECT_BARRIER
    if getattr(operation, "may_raise"):
        return RegionBoundaryReason.EXCEPTION_BARRIER
    if getattr(operation, "op") in {"branch", "jump"}:
        return RegionBoundaryReason.CONTROL_FLOW
    return None


def _provider_candidates(
    operation_ids: tuple[str, ...],
    operations: dict[str, object],
) -> tuple[str, ...]:
    allowed = {
        "argument",
        "constant",
        "binary.add",
        "binary.sub",
        "binary.mul",
        "return",
    }
    selected = [operations[value] for value in operation_ids]
    if all(
        getattr(operation, "op") in allowed
        and getattr(operation, "result_type") is LogicalType.FLOAT64
        and getattr(operation, "nullability") is Nullability.NON_NULL
        and getattr(operation, "effect") is EffectKind.PURE
        and not getattr(operation, "may_raise")
        for operation in selected
    ):
        return ("scalar_cinderx",)
    return ()


def _region_values(
    operation_ids: tuple[str, ...],
    *,
    operations: dict[str, object],
    definition: dict[str, str],
    uses: dict[str, set[str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected = set(operation_ids)
    entries = {
        operand
        for operation_id in operation_ids
        for operand in getattr(operations[operation_id], "operands")
        if definition.get(operand) not in selected
    }
    exits = {
        result_id
        for operation_id in operation_ids
        if (result_id := getattr(operations[operation_id], "result_id"))
        is not None
        and any(user not in selected for user in uses.get(result_id, set()))
    }
    for operation_id in operation_ids:
        operation = operations[operation_id]
        if getattr(operation, "op") == "return":
            exits.update(getattr(operation, "operands"))
    return tuple(sorted(entries)), tuple(sorted(exits))


def form_semantic_region_graph(
    module: SemanticCoreModule,
) -> SemanticRegionGraph:
    verify_semantic_module(module)
    operations = {
        operation.operation_id: operation
        for operation in module.operations
    }
    definition = {
        operation.result_id: operation.operation_id
        for operation in module.operations
        if operation.result_id is not None
    }
    uses: dict[str, set[str]] = {}
    for operation in module.operations:
        for operand in operation.operands:
            uses.setdefault(operand, set()).add(operation.operation_id)

    provisional_regions: list[
        tuple[str, tuple[str, ...], RegionBoundaryReason, RegionBoundaryReason]
    ] = []
    previous_reason = RegionBoundaryReason.FUNCTION_ENTRY
    for block_index, block in enumerate(module.blocks):
        segment: list[str] = []

        def flush(after: RegionBoundaryReason) -> None:
            nonlocal segment, previous_reason
            if segment:
                provisional_regions.append(
                    (
                        block.block_id,
                        tuple(segment),
                        previous_reason,
                        after,
                    )
                )
                segment = []
                previous_reason = after

        if block_index > 0:
            previous_reason = RegionBoundaryReason.BLOCK_BOUNDARY
        for operation_id in block.operation_ids:
            operation = operations[operation_id]
            reason = _barrier_reason(operation)
            if reason is None:
                segment.append(operation_id)
                continue
            flush(reason)
            before = previous_reason
            provisional_regions.append(
                (
                    block.block_id,
                    (operation_id,),
                    before,
                    reason,
                )
            )
            previous_reason = reason
        flush(
            RegionBoundaryReason.FUNCTION_EXIT
            if block_index == len(module.blocks) - 1
            else RegionBoundaryReason.BLOCK_BOUNDARY
        )

    regions: list[SemanticRegion] = []
    operation_region: dict[str, str] = {}
    for index, (
        block_id,
        operation_ids,
        before,
        after,
    ) in enumerate(provisional_regions):
        region_id = f"region:{index}"
        entry_values, exit_values = _region_values(
            operation_ids,
            operations=operations,
            definition=definition,
            uses=uses,
        )
        region = SemanticRegion(
            region_id,
            block_id,
            operation_ids,
            entry_values,
            exit_values,
            _provider_candidates(operation_ids, operations),
            before,
            after,
        )
        regions.append(region)
        for operation_id in operation_ids:
            operation_region[operation_id] = region_id

    edges: set[tuple[str, str, RegionEdgeKind, str | None]] = set()
    for operation in module.operations:
        target = operation_region[operation.operation_id]
        for operand in operation.operands:
            source_operation = definition.get(operand)
            if source_operation is None:
                continue
            source = operation_region[source_operation]
            if source != target:
                edges.add((source, target, RegionEdgeKind.DATA, operand))
    first_region_by_block = {
        block.block_id: operation_region[block.operation_ids[0]]
        for block in module.blocks
    }
    last_region_by_block = {
        block.block_id: operation_region[block.operation_ids[-1]]
        for block in module.blocks
    }
    for edge in module.control_edges:
        edges.add(
            (
                last_region_by_block[edge.source_block],
                first_region_by_block[edge.target_block],
                RegionEdgeKind.CONTROL,
                None,
            )
        )
    effect_regions = [
        operation_region[operation.operation_id]
        for operation in module.operations
        if operation.effect is not EffectKind.PURE
    ]
    for source, target in zip(effect_regions, effect_regions[1:]):
        if source != target:
            edges.add((source, target, RegionEdgeKind.EFFECT, None))
    exception_regions = [
        operation_region[operation.operation_id]
        for operation in module.operations
        if operation.may_raise
    ]
    for source, target in zip(
        exception_regions,
        exception_regions[1:],
    ):
        if source != target:
            edges.add(
                (
                    source,
                    target,
                    RegionEdgeKind.EXCEPTION_ORDER,
                    None,
                )
            )
    graph_edges = tuple(
        SemanticRegionEdge(*value)
        for value in sorted(
            edges,
            key=lambda value: (
                value[0],
                value[1],
                value[2].value,
                value[3] or "",
            ),
        )
    )
    provisional = SemanticRegionGraph(
        SEMANTIC_REGION_GRAPH_VERSION,
        module.semantic_hash,
        tuple(regions),
        graph_edges,
        "",
    )
    graph = SemanticRegionGraph(
        provisional.format_version,
        provisional.module_hash,
        provisional.regions,
        provisional.edges,
        provisional.recompute_semantic_hash(),
    )
    verify_semantic_region_graph(module, graph)
    return graph


def verify_semantic_region_graph(
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
) -> None:
    verify_semantic_module(module)
    if (
        graph.format_version != SEMANTIC_REGION_GRAPH_VERSION
        or graph.module_hash != module.semantic_hash
        or not graph.regions
    ):
        raise ValueError("invalid semantic region graph identity")
    region_ids = {region.region_id for region in graph.regions}
    if len(region_ids) != len(graph.regions):
        raise ValueError("duplicate semantic region")
    covered = tuple(
        operation_id
        for region in graph.regions
        for operation_id in region.operation_ids
    )
    if covered != tuple(
        operation.operation_id for operation in module.operations
    ):
        raise ValueError("semantic regions do not cover module exactly")
    operations = {
        operation.operation_id: operation
        for operation in module.operations
    }
    for region in graph.regions:
        selected = [operations[value] for value in region.operation_ids]
        if (
            not selected
            or any(
                operation.block_id != region.block_id
                for operation in selected
            )
            or (
                len(selected) > 1
                and any(
                    _barrier_reason(operation) is not None
                    for operation in selected
                )
            )
            or region.provider_candidates
            != _provider_candidates(region.operation_ids, operations)
        ):
            raise ValueError("invalid semantic region boundary")
    edge_keys = {
        (
            edge.source_region,
            edge.target_region,
            edge.kind,
            edge.value_id,
        )
        for edge in graph.edges
    }
    if (
        len(edge_keys) != len(graph.edges)
        or any(
            edge.source_region not in region_ids
            or edge.target_region not in region_ids
            for edge in graph.edges
        )
    ):
        raise ValueError("invalid semantic region edge")
    if graph.recompute_semantic_hash() != graph.semantic_hash:
        raise ValueError("semantic region graph hash mismatch")
