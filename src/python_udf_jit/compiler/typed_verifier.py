from __future__ import annotations

import re
from collections import defaultdict

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    LiteralKind,
    SemanticLiteral,
)
from python_udf_jit.compiler.typed_ir import (
    BOOL,
    FLOAT64,
    INT64,
    MAX_RUNTIME_DEPENDENCIES,
    MAX_TYPED_BLOCKS,
    MAX_TYPED_OPERATIONS,
    TYPED_SEMANTIC_IR_VERSION,
    UNICODE_SCALAR,
    TypeKind,
    TypeSpec,
    TypedBlock,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:%-]{1,128}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_EDGE_KINDS = frozenset({"branch_false", "branch_true", "jump", "exception"})
_TERMINATORS = frozenset({"branch", "jump", "return"})
_SUPPORTED = frozenset(
    {
        "argument",
        "constant",
        "binary.add",
        "binary.sub",
        "binary.mul",
        "binary.truediv",
        "compare.eq",
        "compare.ne",
        "compare.lt",
        "compare.le",
        "compare.gt",
        "compare.ge",
        "cast",
        "select",
        "sequence.length",
        "sequence.get",
        "mapping.lookup",
        "unicode.property",
        "sequence.builder.create",
        "sequence.builder.append",
        "sequence.builder.finish",
        "branch",
        "jump",
        "return",
    }
)
_UNICODE_PROPERTIES = frozenset(
    {"alnum", "alpha", "decimal", "digit", "numeric", "space"}
)


class TypedVerificationError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code if not detail else f"{reason_code}:{detail}")


def _fail(reason_code: str, detail: str = "") -> None:
    raise TypedVerificationError(reason_code, detail)


def _attributes(operation: TypedOperation) -> dict[str, str]:
    if (
        len(operation.attributes) > 16
        or operation.attributes != tuple(sorted(operation.attributes))
        or len({key for key, _ in operation.attributes}) != len(operation.attributes)
        or any(
            not key
            or len(key.encode("utf-8")) > 128
            or len(value.encode("utf-8")) > 4096
            for key, value in operation.attributes
        )
    ):
        _fail("invalid_attributes", operation.operation_id)
    return dict(operation.attributes)


def _literal_type(literal: SemanticLiteral) -> TypeSpec | None:
    return {
        LiteralKind.NONE: None,
        LiteralKind.BOOL: BOOL,
        LiteralKind.INT64: INT64,
        LiteralKind.FLOAT64: FLOAT64,
        LiteralKind.STRING: None,
        LiteralKind.BYTES: None,
    }[literal.kind]


def _verify_operation_schema(
    operation: TypedOperation,
    *,
    value_types: dict[str, TypeSpec],
    input_types: tuple[TypeSpec, ...],
    block_ids: set[str],
) -> None:
    attributes = _attributes(operation)
    operand_types = tuple(value_types[value] for value in operation.operands)
    if operation.op == "argument":
        try:
            index = int(attributes["index"])
        except (KeyError, ValueError):
            _fail("invalid_argument_index", operation.operation_id)
        if (
            operation.operands
            or operation.literal is not None
            or not 0 <= index < len(input_types)
            or operation.result_type != input_types[index]
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "constant":
        if operation.operands or operation.literal is None:
            _fail("invalid_literal", operation.operation_id)
        try:
            SemanticLiteral.from_document(operation.literal.to_document())
        except (TypeError, ValueError):
            _fail("invalid_literal", operation.operation_id)
        if _literal_type(operation.literal) != operation.result_type:
            _fail("type_mismatch", operation.operation_id)
    elif operation.op.startswith("binary."):
        if operation.op == "binary.truediv":
            valid_binary = (
                len(operand_types) == 2
                and operand_types[0] == operand_types[1]
                and operand_types[0] in {INT64, FLOAT64}
                and operation.result_type == FLOAT64
            )
        else:
            valid_binary = (
                len(operand_types) == 2
                and operand_types[0] == operand_types[1]
                and operation.result_type == operand_types[0]
                and operation.result_type in {INT64, FLOAT64}
            )
        if not valid_binary:
            _fail("type_mismatch", operation.operation_id)
    elif operation.op.startswith("compare."):
        if (
            len(operand_types) != 2
            or operand_types[0] != operand_types[1]
            or operation.result_type != BOOL
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "cast":
        target = attributes.get("target")
        expected = {"bool": BOOL, "int64": INT64, "float64": FLOAT64}.get(target)
        if len(operand_types) != 1 or operation.result_type != expected:
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "select":
        if (
            len(operand_types) != 3
            or operand_types[0] != BOOL
            or operand_types[1] != operand_types[2]
            or operation.result_type != operand_types[1]
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "sequence.length":
        if (
            len(operand_types) != 1
            or operand_types[0].kind is not TypeKind.SEQUENCE
            or operation.result_type != INT64
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "sequence.get":
        if (
            len(operand_types) != 2
            or operand_types[0].kind is not TypeKind.SEQUENCE
            or operand_types[1] != INT64
            or operation.result_type != operand_types[0].parameters[0]
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "unicode.property":
        if (
            operand_types != (UNICODE_SCALAR,)
            or operation.result_type != BOOL
            or attributes.get("property") not in _UNICODE_PROPERTIES
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "mapping.lookup":
        if (
            len(operand_types) != 2
            or operand_types[0].kind is not TypeKind.MAPPING
            or operand_types[1] != operand_types[0].parameters[0]
            or operation.result_type != operand_types[0].parameters[1]
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "sequence.builder.create":
        if operation.operands or operation.result_type is None or operation.result_type.kind is not TypeKind.BUILDER:
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "sequence.builder.append":
        if (
            len(operand_types) != 2
            or operand_types[0].kind is not TypeKind.BUILDER
            or operand_types[1] != operand_types[0].parameters[0]
            or operation.result_type != operand_types[0]
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "sequence.builder.finish":
        if (
            len(operand_types) != 1
            or operand_types[0].kind is not TypeKind.BUILDER
            or operation.result_type is None
            or operation.result_type.kind is not TypeKind.SEQUENCE
            or operation.result_type.parameters != operand_types[0].parameters
        ):
            _fail("type_mismatch", operation.operation_id)
    elif operation.op == "branch":
        if (
            operand_types != (BOOL,)
            or attributes.get("true_block") not in block_ids
            or attributes.get("false_block") not in block_ids
        ):
            _fail("invalid_control_flow", operation.operation_id)
    elif operation.op == "jump":
        if operation.operands or attributes.get("target_block") not in block_ids:
            _fail("invalid_control_flow", operation.operation_id)
    elif operation.op == "return":
        if len(operand_types) != 1 or operation.literal is not None:
            _fail("invalid_return", operation.operation_id)
    else:
        _fail("unsupported_operation", operation.op)

    if operation.op in _TERMINATORS:
        if operation.result_id is not None or operation.result_type is not None:
            _fail("terminator_has_result", operation.operation_id)
    elif operation.result_id is None or operation.result_type is None:
        _fail("missing_result", operation.operation_id)


def _dominators(
    entry_block: str,
    block_ids: set[str],
    predecessors: dict[str, set[str]],
) -> dict[str, set[str]]:
    result = {
        block_id: ({entry_block} if block_id == entry_block else set(block_ids))
        for block_id in block_ids
    }
    changed = True
    while changed:
        changed = False
        for block_id in block_ids - {entry_block}:
            incoming = predecessors[block_id]
            if not incoming:
                continue
            common = set.intersection(*(result[value] for value in incoming))
            updated = {block_id, *common}
            if updated != result[block_id]:
                result[block_id] = updated
                changed = True
    return result


def _available_at(
    value_id: str,
    *,
    use_block: str,
    use_position: int,
    definitions: dict[str, tuple[str, int]],
    dominators: dict[str, set[str]],
) -> bool:
    definition_block, definition_position = definitions[value_id]
    if definition_block == use_block:
        return definition_position < use_position
    return definition_block in dominators[use_block]


def verify_typed_module(
    module: TypedSemanticModule,
    *,
    max_operations: int = MAX_TYPED_OPERATIONS,
    max_blocks: int = MAX_TYPED_BLOCKS,
) -> None:
    if (
        module.format_version != TYPED_SEMANTIC_IR_VERSION
        or _HASH.fullmatch(module.function_id) is None
        or _HASH.fullmatch(module.semantic_hash) is None
        or not module.input_types
    ):
        _fail("invalid_module")
    if (
        len(module.runtime_dependency_hashes) > MAX_RUNTIME_DEPENDENCIES
        or module.runtime_dependency_hashes
        != tuple(sorted(set(module.runtime_dependency_hashes)))
        or any(
            _HASH.fullmatch(value) is None
            for value in module.runtime_dependency_hashes
        )
    ):
        _fail("invalid_dependencies")
    if not module.operations or len(module.operations) > max_operations:
        _fail("operation_limit")
    if not module.blocks or len(module.blocks) > max_blocks:
        _fail("block_limit")
    try:
        for value in (*module.input_types, module.output_type):
            value.verify()
    except ValueError:
        _fail("invalid_type")

    blocks_by_id = {block.block_id: block for block in module.blocks}
    block_ids = set(blocks_by_id)
    if (
        len(block_ids) != len(module.blocks)
        or module.entry_block not in block_ids
        or any(_SAFE_ID.fullmatch(value) is None for value in block_ids)
        or blocks_by_id[module.entry_block].arguments
    ):
        _fail("invalid_control_flow", "blocks")
    operation_ids = tuple(operation.operation_id for operation in module.operations)
    if (
        len(set(operation_ids)) != len(operation_ids)
        or any(_SAFE_ID.fullmatch(value) is None for value in operation_ids)
        or any(operation.op not in _SUPPORTED for operation in module.operations)
    ):
        _fail("unsupported_operation")
    operations = {value.operation_id: value for value in module.operations}
    flattened = tuple(
        operation_id
        for block in module.blocks
        for operation_id in block.operation_ids
    )
    if (
        flattened != operation_ids
        or any(
            operations[operation_id].block_id != block.block_id
            for block in module.blocks
            for operation_id in block.operation_ids
        )
        or any(
            not block.operation_ids
            or operations[block.operation_ids[-1]].op not in _TERMINATORS
            for block in module.blocks
        )
    ):
        _fail("invalid_control_flow", "block_operations")

    edge_keys = {
        (edge.source_block, edge.target_block, edge.kind)
        for edge in module.control_edges
    }
    if (
        len(edge_keys) != len(module.control_edges)
        or any(
            edge.source_block not in block_ids
            or edge.target_block not in block_ids
            or edge.kind not in _EDGE_KINDS
            for edge in module.control_edges
        )
    ):
        _fail("invalid_control_flow", "edges")
    normal_edges = tuple(edge for edge in module.control_edges if edge.kind != "exception")
    edges_by_source: dict[str, list[TypedControlEdge]] = defaultdict(list)
    predecessors: dict[str, set[str]] = {value: set() for value in block_ids}
    for edge in normal_edges:
        edges_by_source[edge.source_block].append(edge)
        predecessors[edge.target_block].add(edge.source_block)
    for block in module.blocks:
        terminator = operations[block.operation_ids[-1]]
        attributes = dict(terminator.attributes)
        expected = (
            {
                (attributes.get("true_block"), "branch_true"),
                (attributes.get("false_block"), "branch_false"),
            }
            if terminator.op == "branch"
            else {(attributes.get("target_block"), "jump")}
            if terminator.op == "jump"
            else set()
        )
        observed = {
            (edge.target_block, edge.kind)
            for edge in edges_by_source[block.block_id]
        }
        if observed != expected:
            _fail("invalid_control_flow", f"terminator_edges:{block.block_id}")

    reachable = {module.entry_block}
    pending = [module.entry_block]
    while pending:
        current = pending.pop()
        for edge in edges_by_source[current]:
            if edge.target_block not in reachable:
                reachable.add(edge.target_block)
                pending.append(edge.target_block)
    if reachable != block_ids:
        _fail("invalid_control_flow", "unreachable")
    dominators = _dominators(module.entry_block, block_ids, predecessors)

    value_types: dict[str, TypeSpec] = {}
    definitions: dict[str, tuple[str, int]] = {}
    for block in module.blocks:
        for argument in block.arguments:
            try:
                argument.type.verify()
            except ValueError:
                _fail("invalid_type", argument.value_id)
            if argument.value_id in value_types or _SAFE_ID.fullmatch(argument.value_id) is None:
                _fail("duplicate_value", argument.value_id)
            value_types[argument.value_id] = argument.type
            definitions[argument.value_id] = (block.block_id, -1)
        for position, operation_id in enumerate(block.operation_ids):
            operation = operations[operation_id]
            if operation.result_id is None:
                continue
            if operation.result_id in value_types or _SAFE_ID.fullmatch(operation.result_id) is None:
                _fail("duplicate_value", operation.result_id)
            if operation.result_type is None:
                _fail("missing_result", operation.operation_id)
            try:
                operation.result_type.verify()
            except ValueError:
                _fail("invalid_type", operation.operation_id)
            value_types[operation.result_id] = operation.result_type
            definitions[operation.result_id] = (block.block_id, position)

    for edge in normal_edges:
        target = blocks_by_id[edge.target_block]
        if len(edge.arguments) != len(target.arguments):
            _fail("edge_argument_count_mismatch", edge.target_block)
        for value_id, target_argument in zip(edge.arguments, target.arguments, strict=True):
            if value_id not in value_types:
                _fail("unknown_operand", value_id)
            if value_types[value_id] != target_argument.type:
                _fail("edge_argument_type_mismatch", value_id)
            source = blocks_by_id[edge.source_block]
            if not _available_at(
                value_id,
                use_block=edge.source_block,
                use_position=len(source.operation_ids) - 1,
                definitions=definitions,
                dominators=dominators,
            ):
                _fail("operand_not_available", value_id)

    exception_orders: list[int] = []
    for block in module.blocks:
        for position, operation_id in enumerate(block.operation_ids):
            operation = operations[operation_id]
            if operation.source_offset is not None and operation.source_offset < 0:
                _fail("invalid_source_offset", operation.operation_id)
            for operand in operation.operands:
                if operand not in value_types:
                    _fail("unknown_operand", operand)
                if not _available_at(
                    operand,
                    use_block=block.block_id,
                    use_position=position,
                    definitions=definitions,
                    dominators=dominators,
                ):
                    _fail("operand_not_available", operand)
            _verify_operation_schema(
                operation,
                value_types=value_types,
                input_types=module.input_types,
                block_ids=block_ids,
            )
            if operation.may_raise:
                if operation.exception_order is None or operation.exception_order < 0:
                    _fail("invalid_exception_order", operation.operation_id)
                exception_orders.append(operation.exception_order)
            elif operation.exception_order is not None:
                _fail("invalid_exception_order", operation.operation_id)
            if (
                operation.effect is EffectKind.NONDETERMINISTIC
                and operation.determinism is not Determinism.NONDETERMINISTIC
            ):
                _fail("invalid_effect", operation.operation_id)
            if operation.op in {"argument", "constant"} and operation.effect is not EffectKind.PURE:
                _fail("invalid_effect", operation.operation_id)
    if exception_orders != list(range(len(exception_orders))):
        _fail("invalid_exception_order")

    arguments = [value for value in module.operations if value.op == "argument"]
    try:
        argument_indexes = [int(dict(value.attributes)["index"]) for value in arguments]
    except (KeyError, ValueError):
        _fail("invalid_argument_index")
    if argument_indexes != list(range(len(module.input_types))):
        _fail("invalid_argument_index")
    returns = [value for value in module.operations if value.op == "return"]
    if (
        len(returns) != 1
        or returns[0].operation_id != module.return_operation_id
        or value_types[returns[0].operands[0]] != module.output_type
    ):
        _fail("invalid_return")
    if module.recompute_semantic_hash() != module.semantic_hash:
        _fail("semantic_hash_mismatch")
