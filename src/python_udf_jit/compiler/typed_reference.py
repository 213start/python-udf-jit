from __future__ import annotations

import operator
from bisect import bisect_left
from collections.abc import Mapping, Sequence

from python_udf_jit.compiler.typed_ir import (
    BOOL,
    FLOAT64,
    INT64,
    UNICODE_SCALAR,
    Exactness,
    TypeKind,
    TypeSpec,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
    decode_int_table,
)
from python_udf_jit.compiler.typed_verifier import verify_typed_module


_BINARY = {
    "binary.add": operator.add,
    "binary.sub": operator.sub,
    "binary.mul": operator.mul,
    "binary.truediv": operator.truediv,
}
_COMPARE = {
    "compare.eq": operator.eq,
    "compare.ne": operator.ne,
    "compare.lt": operator.lt,
    "compare.le": operator.le,
    "compare.gt": operator.gt,
    "compare.ge": operator.ge,
}
_UNICODE_PROPERTY = {
    "alnum": str.isalnum,
    "alpha": str.isalpha,
    "decimal": str.isdecimal,
    "digit": str.isdigit,
    "numeric": str.isnumeric,
    "space": str.isspace,
}


def _exact_python_type(type_spec: TypeSpec) -> type[object] | None:
    if type_spec.kind is TypeKind.SEQUENCE:
        return {"str": str, "list": list, "tuple": tuple, "range": range}.get(
            type_spec.name
        )
    if type_spec.kind is TypeKind.MAPPING:
        return {"dict": dict}.get(type_spec.name)
    if type_spec.kind is TypeKind.PYTHON_OBJECT:
        return {"object": object}.get(type_spec.name)
    return None


def _guard_value(value: object, type_spec: TypeSpec) -> None:
    if value is None:
        if type_spec.nullable:
            return
        raise TypeError("nullability_guard_failed")
    if type_spec == BOOL:
        accepted = type(value) is bool
    elif type_spec == INT64:
        accepted = type(value) is int and -(1 << 63) <= value < (1 << 63)
    elif type_spec == FLOAT64:
        accepted = type(value) is float
    elif type_spec == UNICODE_SCALAR:
        accepted = type(value) is str and len(value) == 1
    else:
        expected = _exact_python_type(type_spec)
        if type_spec.exactness is Exactness.EXACT and expected is not None:
            accepted = type(value) is expected
        elif expected is not None:
            accepted = isinstance(value, expected)
        else:
            accepted = True
    if not accepted:
        raise TypeError("exact_type_guard_failed")


def _execute_operation(
    operation: TypedOperation,
    values: dict[str, object],
    inputs: tuple[object, ...],
) -> None:
    arguments = tuple(values[value] for value in operation.operands)
    if operation.op == "argument":
        result: object = inputs[int(operation.attribute("index") or "-1")]
    elif operation.op == "constant":
        if operation.literal is None:
            raise RuntimeError("verified constant lost its literal")
        result = operation.literal.value
    elif operation.op in _BINARY:
        result = _BINARY[operation.op](*arguments)
    elif operation.op in _COMPARE:
        result = _COMPARE[operation.op](*arguments)
    elif operation.op == "cast":
        cast = {"bool": bool, "int64": int, "float64": float}[
            operation.attribute("target") or ""
        ]
        result = cast(arguments[0])
    elif operation.op == "select":
        result = arguments[1] if arguments[0] else arguments[2]
    elif operation.op == "sequence.length":
        result = len(arguments[0])  # type: ignore[arg-type]
    elif operation.op == "sequence.get":
        result = arguments[0][arguments[1]]  # type: ignore[index]
    elif operation.op == "unicode.property":
        result = _UNICODE_PROPERTY[operation.attribute("property") or ""](
            arguments[0]  # type: ignore[arg-type]
        )
    elif operation.op == "mapping.lookup":
        result = arguments[0][arguments[1]]  # type: ignore[index]
    elif operation.op == "immutable.lookup":
        keys = decode_int_table(
            operation.attribute("keys") or "",
            max_items=256,
        )
        replacements = decode_int_table(
            operation.attribute("values") or "",
            max_items=256,
        )
        codepoint = ord(arguments[0])  # type: ignore[arg-type]
        table_index = bisect_left(keys, codepoint)
        if table_index == len(keys) or keys[table_index] != codepoint:
            result = arguments[1]
        else:
            result = chr(replacements[table_index])
    elif operation.op == "fsm.transition":
        state_count = int(operation.attribute("state_count") or "0")
        class_count = int(operation.attribute("class_count") or "0")
        transitions = decode_int_table(
            operation.attribute("transitions") or "",
            max_items=128,
            maximum=state_count - 1,
        )
        result = transitions[
            int(arguments[0]) * class_count + int(arguments[1])
        ]
    elif operation.op == "sequence.builder.create":
        result = []
    elif operation.op == "sequence.builder.append":
        builder = list(arguments[0])  # persistent reference semantics
        builder.append(arguments[1])
        result = builder
    elif operation.op == "sequence.builder.apply":
        class_count = int(operation.attribute("class_count") or "0")
        actions = decode_int_table(
            operation.attribute("actions") or "",
            max_items=128,
            maximum=4,
        )
        emissions = decode_int_table(
            operation.attribute("emissions") or "",
            max_items=128,
        )
        table_index = (
            int(arguments[2]) * class_count + int(arguments[3])
        )
        action = actions[table_index]
        builder = list(arguments[0])  # persistent reference semantics
        if action in {2, 3}:
            builder.append(chr(emissions[table_index]))
        if action in {1, 3, 4}:
            builder.append(arguments[1])
        if action == 4:
            builder.append(chr(emissions[table_index]))
        result = builder
    elif operation.op == "sequence.builder.finish":
        if operation.result_type is None:
            raise RuntimeError("verified builder finish lost its type")
        if operation.result_type.name == "str":
            result = "".join(arguments[0])  # type: ignore[arg-type]
        elif operation.result_type.name == "tuple":
            result = tuple(arguments[0])  # type: ignore[arg-type]
        else:
            result = list(arguments[0])  # type: ignore[arg-type]
    else:
        raise RuntimeError(f"unsupported typed reference operation:{operation.op}")
    if operation.result_id is None or operation.result_type is None:
        raise RuntimeError("verified operation lost its result")
    _guard_value(result, operation.result_type)
    values[operation.result_id] = result


def _select_edge(
    edges: tuple[TypedControlEdge, ...],
    operation: TypedOperation,
    values: dict[str, object],
) -> TypedControlEdge:
    if operation.op == "jump":
        kind = "jump"
    else:
        kind = "branch_true" if values[operation.operands[0]] else "branch_false"
    matches = tuple(edge for edge in edges if edge.kind == kind)
    if len(matches) != 1:
        raise RuntimeError("verified control edge is ambiguous")
    return matches[0]


def execute_typed_module(
    module: TypedSemanticModule,
    inputs: tuple[object, ...],
    *,
    max_steps: int = 1_000_000,
) -> object:
    verify_typed_module(module)
    if len(inputs) != len(module.input_types):
        raise TypeError("typed reference input count mismatch")
    if max_steps <= 0:
        raise ValueError("typed reference step budget must be positive")
    for value, type_spec in zip(inputs, module.input_types, strict=True):
        _guard_value(value, type_spec)

    blocks = {value.block_id: value for value in module.blocks}
    operations = {value.operation_id: value for value in module.operations}
    edges_by_source = {
        block_id: tuple(
            edge
            for edge in module.control_edges
            if edge.source_block == block_id and edge.kind != "exception"
        )
        for block_id in blocks
    }
    values: dict[str, object] = {}
    current_block = module.entry_block
    pending_arguments: tuple[object, ...] = ()
    steps = 0
    while True:
        block = blocks[current_block]
        if len(pending_arguments) != len(block.arguments):
            raise RuntimeError("verified block argument count changed")
        for argument, value in zip(block.arguments, pending_arguments, strict=True):
            _guard_value(value, argument.type)
            values[argument.value_id] = value
        for operation_id in block.operation_ids:
            steps += 1
            if steps > max_steps:
                raise RuntimeError("typed reference step budget exceeded")
            operation = operations[operation_id]
            if operation.op == "return":
                result = values[operation.operands[0]]
                _guard_value(result, module.output_type)
                return result
            if operation.op in {"branch", "jump"}:
                edge = _select_edge(edges_by_source[current_block], operation, values)
                pending_arguments = tuple(values[value] for value in edge.arguments)
                current_block = edge.target_block
                break
            _execute_operation(operation, values, inputs)
        else:
            raise RuntimeError("verified block did not terminate")
