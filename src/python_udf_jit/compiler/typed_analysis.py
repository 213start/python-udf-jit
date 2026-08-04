from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum

from python_udf_jit.compiler.typed_ir import (
    TypeSpec,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
)
from python_udf_jit.compiler.typed_verifier import verify_typed_module


TYPED_ANALYSIS_VERSION = 2


def _hash(prefix: bytes, document: object) -> str:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(prefix + encoded).hexdigest()


class WorkDimension(StrEnum):
    COMPUTE = "compute"
    CONTROL = "control"
    OBJECT = "object"
    DISPATCH = "dispatch"
    SUSPEND = "suspend"
    DYNAMIC = "dynamic"


class BehaviorFamily(StrEnum):
    NUMERIC_LOOP = "numeric_loop"
    BRANCH_FSM = "branch_fsm"
    SEQUENCE_TRANSFORM = "sequence_transform"
    OBJECT_MANIPULATOR = "object_manipulator"
    CALL_DISPATCHER = "call_dispatcher"
    ASYNC_STATE_MACHINE = "async_state_machine"
    REFLECTION_META = "reflection_meta"
    TRIVIAL = "trivial"
    MIXED = "mixed"


@dataclass(frozen=True)
class BehaviorProfile:
    format_version: int
    module_hash: str
    family: BehaviorFamily
    work_dimension_counts: tuple[tuple[WorkDimension, int], ...]
    loop_count: int
    loop_nesting_depth: int
    backedge_count: int
    code_size_bucket: str
    risk_reasons: tuple[str, ...]
    estimated_boxing_edges: int
    estimated_dynamic_dispatches: int
    profile_hash: str

    def count(self, dimension: WorkDimension) -> int:
        return dict(self.work_dimension_counts).get(dimension, 0)

    def semantic_document(self) -> dict[str, object]:
        return {
            "backedge_count": self.backedge_count,
            "code_size_bucket": self.code_size_bucket,
            "estimated_boxing_edges": self.estimated_boxing_edges,
            "estimated_dynamic_dispatches": self.estimated_dynamic_dispatches,
            "family": self.family.value,
            "format_version": self.format_version,
            "loop_count": self.loop_count,
            "loop_nesting_depth": self.loop_nesting_depth,
            "module_hash": self.module_hash,
            "risk_reasons": list(self.risk_reasons),
            "work_dimension_counts": [
                [dimension.value, count]
                for dimension, count in self.work_dimension_counts
            ],
        }

    def recompute_hash(self) -> str:
        return _hash(
            b"python-udf-jit-behavior-profile-v1\0",
            self.semantic_document(),
        )

    def to_document(self) -> dict[str, object]:
        return {**self.semantic_document(), "profile_hash": self.profile_hash}

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        module_hash: str,
    ) -> "BehaviorProfile":
        expected = {
            "backedge_count",
            "code_size_bucket",
            "estimated_boxing_edges",
            "estimated_dynamic_dispatches",
            "family",
            "format_version",
            "loop_count",
            "loop_nesting_depth",
            "module_hash",
            "profile_hash",
            "risk_reasons",
            "work_dimension_counts",
        }
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or document["module_hash"] != module_hash
            or document["format_version"] != TYPED_ANALYSIS_VERSION
            or not isinstance(document["risk_reasons"], list)
            or any(not isinstance(value, str) for value in document["risk_reasons"])
            or not isinstance(document["work_dimension_counts"], list)
        ):
            raise ValueError("invalid behavior profile")
        counts: list[tuple[WorkDimension, int]] = []
        for value in document["work_dimension_counts"]:
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not isinstance(value[0], str)
                or type(value[1]) is not int
                or value[1] < 0
            ):
                raise ValueError("invalid behavior work dimension")
            counts.append((WorkDimension(value[0]), value[1]))
        integers = (
            document["loop_count"],
            document["loop_nesting_depth"],
            document["backedge_count"],
            document["estimated_boxing_edges"],
            document["estimated_dynamic_dispatches"],
        )
        if (
            any(type(value) is not int or value < 0 for value in integers)
            or not isinstance(document["code_size_bucket"], str)
            or not isinstance(document["profile_hash"], str)
        ):
            raise ValueError("invalid behavior profile value")
        result = cls(
            TYPED_ANALYSIS_VERSION,
            module_hash,
            BehaviorFamily(document["family"]),
            tuple(counts),
            document["loop_count"],
            document["loop_nesting_depth"],
            document["backedge_count"],
            document["code_size_bucket"],
            tuple(document["risk_reasons"]),
            document["estimated_boxing_edges"],
            document["estimated_dynamic_dispatches"],
            document["profile_hash"],
        )
        if (
            tuple(dimension for dimension, _ in result.work_dimension_counts)
            != tuple(sorted(WorkDimension, key=lambda value: value.value))
            or result.recompute_hash() != result.profile_hash
        ):
            raise ValueError("behavior profile verification failed")
        return result


@dataclass(frozen=True)
class TypeEvidenceEntry:
    value_id: str
    type: TypeSpec
    source: str
    requires_guard: bool

    def to_document(self) -> dict[str, object]:
        return {
            "requires_guard": self.requires_guard,
            "source": self.source,
            "type": self.type.to_document(),
            "value_id": self.value_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "TypeEvidenceEntry":
        if (
            not isinstance(document, dict)
            or set(document) != {"requires_guard", "source", "type", "value_id"}
            or type(document["requires_guard"]) is not bool
            or not isinstance(document["source"], str)
            or not isinstance(document["value_id"], str)
        ):
            raise ValueError("invalid type evidence entry")
        return cls(
            document["value_id"],
            TypeSpec.from_document(document["type"]),
            document["source"],
            document["requires_guard"],
        )


@dataclass(frozen=True)
class TypeEvidence:
    format_version: int
    module_hash: str
    entries: tuple[TypeEvidenceEntry, ...]
    evidence_hash: str

    def semantic_document(self) -> dict[str, object]:
        return {
            "entries": [value.to_document() for value in self.entries],
            "format_version": self.format_version,
            "module_hash": self.module_hash,
        }

    def recompute_hash(self) -> str:
        return _hash(
            b"python-udf-jit-type-evidence-v1\0",
            self.semantic_document(),
        )

    def to_document(self) -> dict[str, object]:
        return {**self.semantic_document(), "evidence_hash": self.evidence_hash}

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        module_hash: str,
    ) -> "TypeEvidence":
        if (
            not isinstance(document, dict)
            or set(document) != {"entries", "evidence_hash", "format_version", "module_hash"}
            or document["format_version"] != TYPED_ANALYSIS_VERSION
            or document["module_hash"] != module_hash
            or not isinstance(document["entries"], list)
            or not isinstance(document["evidence_hash"], str)
        ):
            raise ValueError("invalid type evidence")
        result = cls(
            TYPED_ANALYSIS_VERSION,
            module_hash,
            tuple(TypeEvidenceEntry.from_document(value) for value in document["entries"]),
            document["evidence_hash"],
        )
        if (
            len({entry.value_id for entry in result.entries}) != len(result.entries)
            or result.recompute_hash() != result.evidence_hash
        ):
            raise ValueError("type evidence verification failed")
        return result


@dataclass(frozen=True)
class LoopPattern:
    header: str
    latches: tuple[str, ...]
    blocks: tuple[str, ...]
    depth: int
    kind: str

    def to_document(self) -> dict[str, object]:
        return {
            "blocks": list(self.blocks),
            "depth": self.depth,
            "header": self.header,
            "kind": self.kind,
            "latches": list(self.latches),
        }

    @classmethod
    def from_document(cls, document: object) -> "LoopPattern":
        if (
            not isinstance(document, dict)
            or set(document) != {"blocks", "depth", "header", "kind", "latches"}
            or not isinstance(document["blocks"], list)
            or not isinstance(document["latches"], list)
            or any(not isinstance(value, str) for value in (*document["blocks"], *document["latches"]))
            or not isinstance(document["header"], str)
            or not isinstance(document["kind"], str)
            or type(document["depth"]) is not int
            or document["depth"] <= 0
        ):
            raise ValueError("invalid loop pattern")
        return cls(
            document["header"],
            tuple(document["latches"]),
            tuple(document["blocks"]),
            document["depth"],
            document["kind"],
        )


@dataclass(frozen=True)
class ReductionPattern:
    header: str
    accumulator: str
    update_value: str
    operation: str

    def to_document(self) -> dict[str, str]:
        return {
            "accumulator": self.accumulator,
            "header": self.header,
            "operation": self.operation,
            "update_value": self.update_value,
        }

    @classmethod
    def from_document(cls, document: object) -> "ReductionPattern":
        expected = {"accumulator", "header", "operation", "update_value"}
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or any(not isinstance(document[value], str) for value in expected)
        ):
            raise ValueError("invalid reduction pattern")
        return cls(
            document["header"],
            document["accumulator"],
            document["update_value"],
            document["operation"],
        )


@dataclass(frozen=True)
class PatternAnalysis:
    format_version: int
    module_hash: str
    loops: tuple[LoopPattern, ...]
    reductions: tuple[ReductionPattern, ...]
    immutable_lookup_operations: tuple[str, ...]
    builder_operations: tuple[str, ...]
    fsm_operations: tuple[str, ...]
    pattern_hash: str

    def semantic_document(self) -> dict[str, object]:
        return {
            "builder_operations": list(self.builder_operations),
            "format_version": self.format_version,
            "fsm_operations": list(self.fsm_operations),
            "immutable_lookup_operations": list(self.immutable_lookup_operations),
            "loops": [value.to_document() for value in self.loops],
            "module_hash": self.module_hash,
            "reductions": [value.to_document() for value in self.reductions],
        }

    def recompute_hash(self) -> str:
        return _hash(
            b"python-udf-jit-pattern-analysis-v1\0",
            self.semantic_document(),
        )

    def to_document(self) -> dict[str, object]:
        return {**self.semantic_document(), "pattern_hash": self.pattern_hash}

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        module_hash: str,
    ) -> "PatternAnalysis":
        expected = {
            "builder_operations",
            "format_version",
            "fsm_operations",
            "immutable_lookup_operations",
            "loops",
            "module_hash",
            "pattern_hash",
            "reductions",
        }
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or document["format_version"] != TYPED_ANALYSIS_VERSION
            or document["module_hash"] != module_hash
            or any(
                not isinstance(document[name], list)
                for name in (
                    "builder_operations",
                    "fsm_operations",
                    "immutable_lookup_operations",
                    "loops",
                    "reductions",
                )
            )
            or not isinstance(document["pattern_hash"], str)
        ):
            raise ValueError("invalid pattern analysis")
        result = cls(
            TYPED_ANALYSIS_VERSION,
            module_hash,
            tuple(LoopPattern.from_document(value) for value in document["loops"]),
            tuple(
                ReductionPattern.from_document(value)
                for value in document["reductions"]
            ),
            tuple(document["immutable_lookup_operations"]),
            tuple(document["builder_operations"]),
            tuple(document["fsm_operations"]),
            document["pattern_hash"],
        )
        if (
            any(
                not isinstance(value, str)
                for value in (
                    *result.immutable_lookup_operations,
                    *result.builder_operations,
                    *result.fsm_operations,
                )
            )
            or result.recompute_hash() != result.pattern_hash
        ):
            raise ValueError("pattern analysis verification failed")
        return result


@dataclass(frozen=True)
class TypedAnalysisBundle:
    module_hash: str
    behavior: BehaviorProfile
    type_evidence: TypeEvidence
    patterns: PatternAnalysis
    analysis_hash: str

    def semantic_document(self) -> dict[str, str]:
        return {
            "behavior_hash": self.behavior.profile_hash,
            "module_hash": self.module_hash,
            "pattern_hash": self.patterns.pattern_hash,
            "type_evidence_hash": self.type_evidence.evidence_hash,
        }

    def recompute_hash(self) -> str:
        return _hash(
            b"python-udf-jit-typed-analysis-bundle-v1\0",
            self.semantic_document(),
        )

    def to_documents(self) -> dict[str, object]:
        return {
            "analysis": {
                **self.semantic_document(),
                "analysis_hash": self.analysis_hash,
            },
            "behavior-profile": self.behavior.to_document(),
            "pattern-analysis": self.patterns.to_document(),
            "type-evidence": self.type_evidence.to_document(),
        }

    @classmethod
    def from_documents(
        cls,
        documents: object,
        *,
        module: TypedSemanticModule,
    ) -> "TypedAnalysisBundle":
        verify_typed_module(module)
        if (
            not isinstance(documents, dict)
            or set(documents)
            != {"analysis", "behavior-profile", "pattern-analysis", "type-evidence"}
            or not isinstance(documents["analysis"], dict)
        ):
            raise ValueError("invalid typed analysis documents")
        behavior = BehaviorProfile.from_document(
            documents["behavior-profile"],
            module_hash=module.semantic_hash,
        )
        patterns = PatternAnalysis.from_document(
            documents["pattern-analysis"],
            module_hash=module.semantic_hash,
        )
        type_evidence = TypeEvidence.from_document(
            documents["type-evidence"],
            module_hash=module.semantic_hash,
        )
        analysis = documents["analysis"]
        expected = {
            "analysis_hash",
            "behavior_hash",
            "module_hash",
            "pattern_hash",
            "type_evidence_hash",
        }
        if (
            set(analysis) != expected
            or analysis["module_hash"] != module.semantic_hash
            or analysis["behavior_hash"] != behavior.profile_hash
            or analysis["pattern_hash"] != patterns.pattern_hash
            or analysis["type_evidence_hash"] != type_evidence.evidence_hash
            or not isinstance(analysis["analysis_hash"], str)
        ):
            raise ValueError("typed analysis hash links are invalid")
        result = cls(
            module.semantic_hash,
            behavior,
            type_evidence,
            patterns,
            analysis["analysis_hash"],
        )
        if result.recompute_hash() != result.analysis_hash:
            raise ValueError("typed analysis bundle verification failed")
        return result


def _graph(
    module: TypedSemanticModule,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    successors = {block.block_id: set() for block in module.blocks}
    predecessors = {block.block_id: set() for block in module.blocks}
    for edge in module.control_edges:
        if edge.kind == "exception":
            continue
        successors[edge.source_block].add(edge.target_block)
        predecessors[edge.target_block].add(edge.source_block)
    return successors, predecessors


def _dominators(
    module: TypedSemanticModule,
    predecessors: dict[str, set[str]],
) -> dict[str, set[str]]:
    block_ids = set(predecessors)
    result = {
        block_id: (
            {module.entry_block}
            if block_id == module.entry_block
            else set(block_ids)
        )
        for block_id in block_ids
    }
    changed = True
    while changed:
        changed = False
        for block_id in block_ids - {module.entry_block}:
            incoming = predecessors[block_id]
            common = set.intersection(*(result[value] for value in incoming))
            updated = {block_id, *common}
            if updated != result[block_id]:
                result[block_id] = updated
                changed = True
    return result


def _natural_loop(
    header: str,
    latch: str,
    predecessors: dict[str, set[str]],
) -> set[str]:
    result = {header, latch}
    pending = [latch]
    while pending:
        block_id = pending.pop()
        for predecessor in predecessors[block_id]:
            if predecessor not in result:
                result.add(predecessor)
                pending.append(predecessor)
    return result


def _loop_patterns(
    module: TypedSemanticModule,
) -> tuple[tuple[LoopPattern, ...], tuple[TypedControlEdge, ...]]:
    _, predecessors = _graph(module)
    dominators = _dominators(module, predecessors)
    backedges = tuple(
        edge
        for edge in module.control_edges
        if edge.kind != "exception"
        and edge.target_block in dominators[edge.source_block]
    )
    grouped: dict[str, list[TypedControlEdge]] = defaultdict(list)
    for edge in backedges:
        grouped[edge.target_block].append(edge)
    operations = {value.operation_id: value for value in module.operations}
    provisional: list[tuple[str, tuple[str, ...], set[str], str]] = []
    for header, edges in sorted(grouped.items()):
        blocks: set[str] = {header}
        for edge in edges:
            blocks.update(_natural_loop(header, edge.source_block, predecessors))
        loop_operations = tuple(
            operations[operation_id]
            for block in module.blocks
            if block.block_id in blocks
            for operation_id in block.operation_ids
        )
        kind = (
            "iterator_loop"
            if any(value.op == "sequence.get" for value in loop_operations)
            else "loop"
        )
        provisional.append(
            (
                header,
                tuple(sorted(edge.source_block for edge in edges)),
                blocks,
                kind,
            )
        )
    result = tuple(
        LoopPattern(
            header,
            latches,
            tuple(sorted(blocks)),
            1
            + sum(
                blocks < outer_blocks
                for _, _, outer_blocks, _ in provisional
            ),
            kind,
        )
        for header, latches, blocks, kind in provisional
    )
    return result, backedges


def _reduction_patterns(
    module: TypedSemanticModule,
    loops: tuple[LoopPattern, ...],
    backedges: tuple[TypedControlEdge, ...],
) -> tuple[ReductionPattern, ...]:
    blocks = {value.block_id: value for value in module.blocks}
    operations = {value.operation_id: value for value in module.operations}
    definitions = {
        value.result_id: value
        for value in module.operations
        if value.result_id is not None
    }
    uses: dict[str, list[TypedOperation]] = defaultdict(list)
    for operation in module.operations:
        for operand in operation.operands:
            uses[operand].append(operation)
    result: list[ReductionPattern] = []
    for loop in loops:
        header = blocks[loop.header]
        latch_edges = tuple(
            edge
            for edge in backedges
            if edge.target_block == loop.header
        )
        for argument_index, argument in enumerate(header.arguments):
            induction = any(
                operation.op == "sequence.get"
                and len(operation.operands) == 2
                and operation.operands[1] == argument.value_id
                for operation in uses[argument.value_id]
            )
            if induction:
                continue
            updates: list[tuple[str, TypedOperation]] = []
            for edge in latch_edges:
                value_id = edge.arguments[argument_index]
                operation = definitions.get(value_id)
                if (
                    operation is not None
                    and operation.op
                    in {"binary.add", "binary.mul", "compare.lt", "compare.gt"}
                    and argument.value_id in operation.operands
                ):
                    updates.append((value_id, operation))
            if len(updates) == len(latch_edges) == 1:
                value_id, operation = updates[0]
                result.append(
                    ReductionPattern(
                        loop.header,
                        argument.value_id,
                        value_id,
                        operation.op,
                    )
                )
    return tuple(result)


def _operation_dimension(operation: TypedOperation) -> WorkDimension:
    if operation.op in {"branch", "jump", "return"}:
        return WorkDimension.CONTROL
    if operation.op.startswith("sequence.builder") or operation.op in {
        "sequence.get",
        "mapping.lookup",
        "immutable.lookup",
    }:
        return WorkDimension.OBJECT
    if operation.op == "argument" and operation.result_type is not None and operation.result_type.name == "object":
        return WorkDimension.DYNAMIC
    return WorkDimension.COMPUTE


def _type_evidence(module: TypedSemanticModule) -> TypeEvidence:
    sources: dict[str, str] = {}
    types: dict[str, TypeSpec] = {}
    for block in module.blocks:
        for argument in block.arguments:
            sources[argument.value_id] = "block_argument"
            types[argument.value_id] = argument.type
    for operation in module.operations:
        if operation.result_id is None or operation.result_type is None:
            continue
        sources[operation.result_id] = (
            "static_schema" if operation.op == "argument" else "operation_inference"
        )
        types[operation.result_id] = operation.result_type
    entries = tuple(
        TypeEvidenceEntry(
            value_id,
            types[value_id],
            sources[value_id],
            types[value_id].requires_guard,
        )
        for value_id in sorted(types)
    )
    provisional = TypeEvidence(
        TYPED_ANALYSIS_VERSION,
        module.semantic_hash,
        entries,
        "",
    )
    return TypeEvidence(
        provisional.format_version,
        provisional.module_hash,
        provisional.entries,
        provisional.recompute_hash(),
    )


def _behavior_family(
    *,
    has_reductions: bool,
    has_loops: bool,
    has_fsm: bool,
    has_builders: bool,
    object_operations: int,
    operation_count: int,
) -> BehaviorFamily:
    if has_reductions:
        return BehaviorFamily.NUMERIC_LOOP
    if has_loops and has_fsm:
        return BehaviorFamily.BRANCH_FSM
    if has_loops and has_builders:
        return BehaviorFamily.SEQUENCE_TRANSFORM
    if has_loops:
        return BehaviorFamily.MIXED
    if object_operations:
        return BehaviorFamily.OBJECT_MANIPULATOR
    if operation_count <= 4:
        return BehaviorFamily.TRIVIAL
    return BehaviorFamily.MIXED


def _code_size_bucket(operation_count: int) -> str:
    if operation_count <= 32:
        return "small"
    if operation_count <= 256:
        return "medium"
    return "large"


def _analyze_verified_typed_module(
    module: TypedSemanticModule,
) -> TypedAnalysisBundle:
    """Analyze a module already accepted by ``verify_typed_module``."""

    loops, backedges = _loop_patterns(module)
    reductions = _reduction_patterns(module, loops, backedges)
    counts = {dimension: 0 for dimension in WorkDimension}
    for operation in module.operations:
        counts[_operation_dimension(operation)] += 1
    operation_count = len(module.operations)
    family = _behavior_family(
        has_reductions=bool(reductions),
        has_loops=bool(loops),
        has_fsm=any(operation.op == "fsm.transition" for operation in module.operations),
        has_builders=any(
            operation.op.startswith("sequence.builder")
            for operation in module.operations
        ),
        object_operations=counts[WorkDimension.OBJECT],
        operation_count=operation_count,
    )
    risk_reasons = tuple(
        value
        for value, present in (
            ("exception", any(operation.may_raise for operation in module.operations)),
            ("dynamic", counts[WorkDimension.DYNAMIC] > 0),
        )
        if present
    )
    provisional_behavior = BehaviorProfile(
        TYPED_ANALYSIS_VERSION,
        module.semantic_hash,
        family,
        tuple((value, counts[value]) for value in sorted(WorkDimension, key=lambda item: item.value)),
        len(loops),
        max((loop.depth for loop in loops), default=0),
        len(backedges),
        _code_size_bucket(operation_count),
        risk_reasons,
        sum(
            operation.result_type is not None
            and operation.result_type.representation == "python_object"
            for operation in module.operations
        ),
        counts[WorkDimension.DISPATCH] + counts[WorkDimension.DYNAMIC],
        "",
    )
    behavior = replace(
        provisional_behavior,
        profile_hash=provisional_behavior.recompute_hash(),
    )
    provisional_patterns = PatternAnalysis(
        TYPED_ANALYSIS_VERSION,
        module.semantic_hash,
        loops,
        reductions,
        tuple(
            operation.operation_id
            for operation in module.operations
            if operation.op in {"mapping.lookup", "immutable.lookup"}
        ),
        tuple(
            operation.operation_id
            for operation in module.operations
            if operation.op.startswith("sequence.builder")
        ),
        tuple(
            operation.operation_id
            for operation in module.operations
            if operation.op == "fsm.transition"
        ),
        "",
    )
    patterns = replace(
        provisional_patterns,
        pattern_hash=provisional_patterns.recompute_hash(),
    )
    type_evidence = _type_evidence(module)
    provisional_bundle = TypedAnalysisBundle(
        module.semantic_hash,
        behavior,
        type_evidence,
        patterns,
        "",
    )
    return replace(
        provisional_bundle,
        analysis_hash=provisional_bundle.recompute_hash(),
    )


def analyze_typed_module(module: TypedSemanticModule) -> TypedAnalysisBundle:
    verify_typed_module(module)
    return _analyze_verified_typed_module(module)
