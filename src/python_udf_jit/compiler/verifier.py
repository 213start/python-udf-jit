from __future__ import annotations

from enum import StrEnum
import re

from python_udf_jit.compiler.core_ir import (
    CORE_IR_VERSION,
    MAX_SEMANTIC_BLOCKS,
    MAX_SEMANTIC_NODES,
    SEMANTIC_CORE_IR_VERSION,
    Determinism,
    EffectKind,
    LiteralKind,
    LogicalType,
    Nullability,
    CoreUdfModule,
    SemanticCoreModule,
    SemanticLiteral,
    SemanticOperation,
)


class VerificationRejectCode(StrEnum):
    INVALID_MODULE = "invalid_module"
    NODE_LIMIT = "node_limit"
    DUPLICATE_VALUE = "duplicate_value"
    INVALID_OPERAND = "invalid_operand"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_RETURN = "invalid_return"
    SEMANTIC_HASH_MISMATCH = "semantic_hash_mismatch"
    REGION_MISMATCH = "region_mismatch"
    BLOCK_LIMIT = "block_limit"
    INVALID_CONTROL_FLOW = "invalid_control_flow"
    INVALID_LITERAL = "invalid_literal"
    INVALID_EFFECT = "invalid_effect"
    INVALID_PYTHON_REGION = "invalid_python_region"
    INVALID_EXCEPTION_ORDER = "invalid_exception_order"
    ATTRIBUTE_LIMIT = "attribute_limit"


class VerificationError(ValueError):
    def __init__(self, code: VerificationRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(code: VerificationRejectCode, detail: str = "") -> None:
    raise VerificationError(code, detail)


def verify_core_module(module: CoreUdfModule, *, max_nodes: int = 256, max_constants: int = 256) -> None:
    if (
        module.format_version != CORE_IR_VERSION
        or module.input_type != "float64"
        or module.output_type != "float64"
        or module.effect != "pure"
    ):
        _fail(VerificationRejectCode.INVALID_MODULE)
    if not module.nodes or len(module.nodes) > max_nodes:
        _fail(VerificationRejectCode.NODE_LIMIT)
    supported_operations = {"arg.load", "const.f64", "add.f64", "sub.f64", "mul.f64", "return"}
    unsupported = next((node.op for node in module.nodes if node.op not in supported_operations), None)
    if unsupported is not None:
        _fail(VerificationRejectCode.UNSUPPORTED_OPERATION, unsupported)

    values: set[str] = set()
    expected_value_index = 0
    return_count = 0
    constant_count = 0
    for index, node in enumerate(module.nodes):
        if node.result_type != "float64":
            _fail(VerificationRejectCode.TYPE_MISMATCH)
        if node.op == "return":
            return_count += 1
            if index != len(module.nodes) - 1 or node.result_id is not None or node.literal is not None:
                _fail(VerificationRejectCode.INVALID_RETURN)
            if len(node.operands) != 1 or node.operands[0] not in values:
                _fail(VerificationRejectCode.INVALID_RETURN)
            continue

        expected_id = f"%{expected_value_index}"
        expected_value_index += 1
        if node.result_id in values:
            _fail(VerificationRejectCode.DUPLICATE_VALUE, str(node.result_id))
        if node.result_id != expected_id:
            _fail(VerificationRejectCode.INVALID_MODULE, "noncanonical_value_id")
        if node.op == "arg.load":
            if values or node.operands or node.literal is not None:
                _fail(VerificationRejectCode.INVALID_MODULE, "arg_load")
        elif node.op == "const.f64":
            constant_count += 1
            if node.operands or type(node.literal) is not float:
                _fail(VerificationRejectCode.TYPE_MISMATCH, "const")
        else:
            if node.literal is not None or len(node.operands) != 2:
                _fail(VerificationRejectCode.INVALID_OPERAND, node.op)
            if any(operand not in values for operand in node.operands):
                _fail(VerificationRejectCode.INVALID_OPERAND, node.op)
        values.add(node.result_id)
    if constant_count > max_constants:
        _fail(VerificationRejectCode.NODE_LIMIT, "constants")
    if return_count != 1 or module.return_value not in values:
        _fail(VerificationRejectCode.INVALID_RETURN)
    if module.nodes[-1].operands != (module.return_value,):
        _fail(VerificationRejectCode.INVALID_RETURN)
    if module.recompute_semantic_hash() != module.semantic_hash:
        _fail(VerificationRejectCode.SEMANTIC_HASH_MISMATCH)


def verify_region(module: CoreUdfModule, region: object) -> None:
    verify_core_module(module)
    required = (
        getattr(region, "entry_values", None) == ("%0",),
        getattr(region, "exit_values", None) == (module.return_value,),
        getattr(region, "operation_indexes", None) == tuple(range(len(module.nodes))),
        getattr(region, "pure", None) is True,
        getattr(region, "single_entry", None) is True,
        getattr(region, "single_exit", None) is True,
        getattr(region, "semantic_hash", None) == module.semantic_hash,
    )
    if not all(required):
        _fail(VerificationRejectCode.REGION_MISMATCH)


_SAFE_SEMANTIC_ID = re.compile(r"^[A-Za-z0-9_.:%-]{1,128}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_EDGE_KINDS = frozenset(
    {
        "fallthrough",
        "branch_false",
        "branch_true",
        "jump",
        "exception",
    }
)
_TERMINATORS = frozenset({"branch", "jump", "return"})
_SUPPORTED_SEMANTIC_OPERATIONS = frozenset(
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
        "null.is_null",
        "cast",
        "select",
        "field.load",
        "tuple.make",
        "list.make",
        "modeled.call",
        "python.region",
        "branch",
        "jump",
        "return",
    }
)


def _valid_semantic_id(value: str) -> bool:
    return _SAFE_SEMANTIC_ID.fullmatch(value) is not None


def _literal_type(operation: SemanticOperation) -> LogicalType | None:
    if operation.literal is None:
        return None
    return {
        LiteralKind.NONE: None,
        LiteralKind.BOOL: LogicalType.BOOL,
        LiteralKind.INT64: LogicalType.INT64,
        LiteralKind.FLOAT64: LogicalType.FLOAT64,
        LiteralKind.STRING: LogicalType.STRING,
        LiteralKind.BYTES: LogicalType.BYTES,
    }[operation.literal.kind]


def _verify_attributes(operation: SemanticOperation) -> dict[str, str]:
    if (
        len(operation.attributes) > 16
        or operation.attributes
        != tuple(sorted(operation.attributes))
        or len({key for key, _ in operation.attributes})
        != len(operation.attributes)
        or any(
            not key
            or len(key.encode("utf-8")) > 128
            or len(value.encode("utf-8")) > 4096
            for key, value in operation.attributes
        )
    ):
        _fail(VerificationRejectCode.ATTRIBUTE_LIMIT, operation.operation_id)
    return dict(operation.attributes)


def _verify_operation_schema(
    operation: SemanticOperation,
    *,
    value_types: dict[str, LogicalType],
    value_nullability: dict[str, Nullability],
    input_types: tuple[LogicalType, ...],
    input_nullability: tuple[Nullability, ...],
    block_ids: set[str],
) -> None:
    attributes = _verify_attributes(operation)
    operand_types = tuple(value_types[value] for value in operation.operands)
    operand_nulls = tuple(
        value_nullability[value] for value in operation.operands
    )
    if operation.op == "argument":
        try:
            index = int(attributes["index"])
        except (KeyError, ValueError) as error:
            _fail(VerificationRejectCode.INVALID_MODULE, "argument_index")
            raise AssertionError from error
        if (
            operation.operands
            or operation.literal is not None
            or not 0 <= index < len(input_types)
            or operation.result_type is not input_types[index]
            or operation.nullability is not input_nullability[index]
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "constant":
        literal_type = _literal_type(operation)
        if operation.operands or operation.literal is None:
            _fail(VerificationRejectCode.INVALID_LITERAL, operation.operation_id)
        try:
            SemanticLiteral.from_document(operation.literal.to_document())
        except (TypeError, ValueError) as error:
            _fail(
                VerificationRejectCode.INVALID_LITERAL,
                operation.operation_id,
            )
            raise AssertionError from error
        if operation.literal.kind is LiteralKind.NONE:
            if operation.nullability is not Nullability.KNOWN_NULL:
                _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
        elif (
            literal_type is not operation.result_type
            or operation.nullability is not Nullability.NON_NULL
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op.startswith("binary."):
        if (
            len(operand_types) != 2
            or operand_types[0] is not operand_types[1]
            or operation.result_type is not operand_types[0]
            or operation.literal is not None
            or operation.result_type
            not in {
                LogicalType.INT64,
                LogicalType.FLOAT64,
                LogicalType.STRING,
                LogicalType.BYTES,
            }
            or (
                operation.op != "binary.add"
                and operation.result_type
                in {LogicalType.STRING, LogicalType.BYTES}
            )
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op.startswith("compare."):
        if (
            len(operand_types) != 2
            or operand_types[0] is not operand_types[1]
            or operation.result_type is not LogicalType.BOOL
            or operation.nullability is not Nullability.NON_NULL
            or operation.literal is not None
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "null.is_null":
        if (
            len(operand_types) != 1
            or operation.result_type is not LogicalType.BOOL
            or operation.nullability is not Nullability.NON_NULL
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "cast":
        if (
            len(operand_types) != 1
            or attributes.get("target") != operation.result_type.value
            or operation.result_type
            not in {
                LogicalType.BOOL,
                LogicalType.INT64,
                LogicalType.FLOAT64,
                LogicalType.STRING,
                LogicalType.BYTES,
            }
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "select":
        if (
            len(operand_types) != 3
            or operand_types[0] is not LogicalType.BOOL
            or operand_types[1] is not operand_types[2]
            or operation.result_type is not operand_types[1]
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "field.load":
        if len(operand_types) != 1 or not attributes.get("field_id"):
            _fail(VerificationRejectCode.INVALID_OPERAND, operation.operation_id)
    elif operation.op in {"tuple.make", "list.make"}:
        expected = (
            LogicalType.TUPLE
            if operation.op == "tuple.make"
            else LogicalType.LIST
        )
        if (
            operation.result_type is not expected
            or operation.nullability is not Nullability.NON_NULL
        ):
            _fail(VerificationRejectCode.TYPE_MISMATCH, operation.operation_id)
    elif operation.op == "modeled.call":
        if not attributes.get("model"):
            _fail(VerificationRejectCode.INVALID_MODULE, "call_model")
    elif operation.op == "python.region":
        if (
            not operation.python_region_id
            or operation.effect is not EffectKind.PYTHON
            or operation.determinism is not Determinism.UNKNOWN
        ):
            _fail(
                VerificationRejectCode.INVALID_PYTHON_REGION,
                operation.operation_id,
            )
    elif operation.op == "branch":
        if (
            len(operand_types) != 1
            or operand_types[0] is not LogicalType.BOOL
            or attributes.get("true_block") not in block_ids
            or attributes.get("false_block") not in block_ids
        ):
            _fail(
                VerificationRejectCode.INVALID_CONTROL_FLOW,
                operation.operation_id,
            )
    elif operation.op == "jump":
        if (
            operation.operands
            or attributes.get("target_block") not in block_ids
        ):
            _fail(
                VerificationRejectCode.INVALID_CONTROL_FLOW,
                operation.operation_id,
            )
    elif operation.op == "return":
        if (
            len(operand_types) != 1
            or operation.result_id is not None
            or operation.literal is not None
        ):
            _fail(VerificationRejectCode.INVALID_RETURN)
    else:
        _fail(VerificationRejectCode.UNSUPPORTED_OPERATION, operation.op)

    if operation.op in _TERMINATORS:
        if operation.result_id is not None:
            _fail(VerificationRejectCode.INVALID_RETURN)
    elif operation.result_id is None:
        _fail(VerificationRejectCode.INVALID_MODULE, "missing_result")
    if (
        operation.nullability is Nullability.NON_NULL
        and any(value is Nullability.KNOWN_NULL for value in operand_nulls)
        and operation.op
        not in {"null.is_null", "select", "python.region", "modeled.call"}
    ):
        _fail(VerificationRejectCode.TYPE_MISMATCH, "null_propagation")


def verify_semantic_module(
    module: SemanticCoreModule,
    *,
    max_nodes: int = MAX_SEMANTIC_NODES,
    max_blocks: int = MAX_SEMANTIC_BLOCKS,
    max_constants: int = 1024,
) -> None:
    if (
        module.format_version != SEMANTIC_CORE_IR_VERSION
        or _HASH.fullmatch(module.function_id) is None
        or _HASH.fullmatch(module.semantic_hash) is None
        or not module.input_types
        or len(module.input_types) != len(module.input_nullability)
    ):
        _fail(VerificationRejectCode.INVALID_MODULE)
    if (
        not module.operations
        or len(module.operations) > max_nodes
    ):
        _fail(VerificationRejectCode.NODE_LIMIT)
    if not module.blocks or len(module.blocks) > max_blocks:
        _fail(VerificationRejectCode.BLOCK_LIMIT)
    if (
        sum(operation.literal is not None for operation in module.operations)
        > max_constants
    ):
        _fail(VerificationRejectCode.NODE_LIMIT, "constants")

    block_ids = {block.block_id for block in module.blocks}
    if (
        len(block_ids) != len(module.blocks)
        or module.entry_block not in block_ids
        or any(not _valid_semantic_id(value) for value in block_ids)
    ):
        _fail(VerificationRejectCode.INVALID_CONTROL_FLOW, "blocks")
    operation_ids = [operation.operation_id for operation in module.operations]
    if (
        len(set(operation_ids)) != len(operation_ids)
        or any(not _valid_semantic_id(value) for value in operation_ids)
        or any(
            operation.op not in _SUPPORTED_SEMANTIC_OPERATIONS
            for operation in module.operations
        )
    ):
        _fail(VerificationRejectCode.UNSUPPORTED_OPERATION)
    operations = {
        operation.operation_id: operation for operation in module.operations
    }
    flattened = tuple(
        operation_id
        for block in module.blocks
        for operation_id in block.operation_ids
    )
    if (
        flattened != tuple(operation_ids)
        or any(
            operations[operation_id].block_id != block.block_id
            for block in module.blocks
            for operation_id in block.operation_ids
        )
    ):
        _fail(VerificationRejectCode.INVALID_MODULE, "block_operations")
    if any(
        not block.operation_ids
        or operations[block.operation_ids[-1]].op not in _TERMINATORS
        for block in module.blocks
    ):
        _fail(VerificationRejectCode.INVALID_CONTROL_FLOW, "terminator")

    edges = {
        (edge.source_block, edge.target_block, edge.kind)
        for edge in module.control_edges
    }
    if (
        len(edges) != len(module.control_edges)
        or any(
            edge.source_block not in block_ids
            or edge.target_block not in block_ids
            or edge.kind not in _CONTROL_EDGE_KINDS
            for edge in module.control_edges
        )
    ):
        _fail(VerificationRejectCode.INVALID_CONTROL_FLOW, "edges")
    normal_edges = {
        (
            edge.source_block,
            edge.target_block,
            edge.kind,
        )
        for edge in module.control_edges
        if edge.kind != "exception"
    }
    expected_normal_edges: set[tuple[str, str, str]] = set()
    for block in module.blocks:
        terminator = operations[block.operation_ids[-1]]
        attributes = dict(terminator.attributes)
        if terminator.op == "branch":
            expected_normal_edges.update(
                {
                    (
                        block.block_id,
                        attributes.get("true_block", ""),
                        "branch_true",
                    ),
                    (
                        block.block_id,
                        attributes.get("false_block", ""),
                        "branch_false",
                    ),
                }
            )
        elif terminator.op == "jump":
            expected_normal_edges.add(
                (
                    block.block_id,
                    attributes.get("target_block", ""),
                    "jump",
                )
            )
    if normal_edges != expected_normal_edges:
        _fail(
            VerificationRejectCode.INVALID_CONTROL_FLOW,
            "terminator_edges",
        )
    reachable = {module.entry_block}
    changed = True
    while changed:
        changed = False
        for edge in module.control_edges:
            if (
                edge.source_block in reachable
                and edge.target_block not in reachable
            ):
                reachable.add(edge.target_block)
                changed = True
    if reachable != block_ids:
        _fail(VerificationRejectCode.INVALID_CONTROL_FLOW, "unreachable")

    values: set[str] = set()
    value_types: dict[str, LogicalType] = {}
    value_nullability: dict[str, Nullability] = {}
    exception_orders: list[int] = []
    for operation in module.operations:
        if operation.source_offset is not None and operation.source_offset < 0:
            _fail(
                VerificationRejectCode.INVALID_MODULE,
                "source_offset",
            )
        if any(operand not in values for operand in operation.operands):
            _fail(
                VerificationRejectCode.INVALID_OPERAND,
                operation.operation_id,
            )
        _verify_operation_schema(
            operation,
            value_types=value_types,
            value_nullability=value_nullability,
            input_types=module.input_types,
            input_nullability=module.input_nullability,
            block_ids=block_ids,
        )
        if operation.may_raise:
            if (
                operation.exception_order is None
                or operation.exception_order < 0
            ):
                _fail(
                    VerificationRejectCode.INVALID_EXCEPTION_ORDER,
                    operation.operation_id,
                )
            exception_orders.append(operation.exception_order)
        elif operation.exception_order is not None:
            _fail(
                VerificationRejectCode.INVALID_EXCEPTION_ORDER,
                operation.operation_id,
            )
        if (
            operation.effect is EffectKind.NONDETERMINISTIC
            and operation.determinism
            is not Determinism.NONDETERMINISTIC
        ):
            _fail(
                VerificationRejectCode.INVALID_EFFECT,
                operation.operation_id,
            )
        if operation.effect is not EffectKind.PURE and operation.op in {
            "argument",
            "constant",
        }:
            _fail(
                VerificationRejectCode.INVALID_EFFECT,
                operation.operation_id,
            )
        if operation.result_id is not None:
            if (
                operation.result_id in values
                or not _valid_semantic_id(operation.result_id)
            ):
                _fail(
                    VerificationRejectCode.DUPLICATE_VALUE,
                    str(operation.result_id),
                )
            values.add(operation.result_id)
            value_types[operation.result_id] = operation.result_type
            value_nullability[operation.result_id] = operation.nullability
    if exception_orders != list(range(len(exception_orders))):
        _fail(VerificationRejectCode.INVALID_EXCEPTION_ORDER)
    try:
        argument_indexes = [
            int(dict(operation.attributes)["index"])
            for operation in module.operations
            if operation.op == "argument"
        ]
    except (KeyError, ValueError) as error:
        _fail(VerificationRejectCode.INVALID_MODULE, "argument_index")
        raise AssertionError from error
    if (
        argument_indexes != list(range(len(module.input_types)))
        or len(set(argument_indexes)) != len(argument_indexes)
    ):
        _fail(VerificationRejectCode.INVALID_MODULE, "arguments")

    returns = [
        operation
        for operation in module.operations
        if operation.op == "return"
    ]
    if (
        len(returns) != 1
        or returns[0].operation_id != module.return_operation_id
        or returns[0].result_type is not module.output_type
        or returns[0].nullability is not module.output_nullability
        or value_types[returns[0].operands[0]] is not module.output_type
        or value_nullability[returns[0].operands[0]]
        is not module.output_nullability
    ):
        _fail(VerificationRejectCode.INVALID_RETURN)

    regions = {region.region_id: region for region in module.python_regions}
    python_operations = {
        operation.python_region_id: operation
        for operation in module.operations
        if operation.op == "python.region"
    }
    if (
        len(regions) != len(module.python_regions)
        or None in python_operations
        or set(regions) != set(python_operations)
    ):
        _fail(VerificationRejectCode.INVALID_PYTHON_REGION)
    for region_id, region in regions.items():
        operation = python_operations[region_id]
        if (
            region.operation_id != operation.operation_id
            or region.live_in != operation.operands
            or region.live_out != (operation.result_id,)
            or region.effect is not operation.effect
            or region.may_raise is not operation.may_raise
            or any(value not in values for value in region.live_out)
            or any(block not in block_ids for block in region.handler_blocks)
            or not _valid_semantic_id(region.resume_id)
            or (
                region.source_start is not None
                and region.source_start < 0
            )
            or (
                region.source_end is not None
                and region.source_end < 0
            )
            or (
                region.source_start is not None
                and region.source_end is not None
                and region.source_end < region.source_start
            )
        ):
            _fail(
                VerificationRejectCode.INVALID_PYTHON_REGION,
                region_id,
            )

    if module.recompute_semantic_hash() != module.semantic_hash:
        _fail(VerificationRejectCode.SEMANTIC_HASH_MISMATCH)
