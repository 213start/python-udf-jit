from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from typing import Any

from python_udf_jit.compiler.core_ir import (
    LogicalType,
    SemanticCoreModule,
    SemanticOperation,
    SemanticPythonRegion,
)
from python_udf_jit.compiler.verifier import verify_semantic_module


PythonRegionExecutor = Callable[
    [SemanticPythonRegion, tuple[object, ...]],
    object,
]


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
_CAST = {
    LogicalType.BOOL: bool,
    LogicalType.INT64: int,
    LogicalType.FLOAT64: float,
    LogicalType.STRING: str,
    LogicalType.BYTES: bytes,
}


def _modeled_call(
    operation: SemanticOperation,
    arguments: tuple[object, ...],
) -> object:
    model = operation.attribute("model")
    if model == "builtin.len":
        return len(arguments[0])  # type: ignore[arg-type]
    if model == "builtin.abs":
        return abs(arguments[0])  # type: ignore[arg-type]
    if model == "str.lower":
        return str.lower(arguments[0])  # type: ignore[arg-type]
    if model == "str.upper":
        return str.upper(arguments[0])  # type: ignore[arg-type]
    if model == "str.startswith":
        return str.startswith(arguments[0], arguments[1])  # type: ignore[arg-type]
    if model == "str.endswith":
        return str.endswith(arguments[0], arguments[1])  # type: ignore[arg-type]
    raise ValueError(f"unsupported modeled call: {model}")


def _execute_operation(
    operation: SemanticOperation,
    *,
    values: dict[str, object],
    inputs: tuple[object, ...],
    python_regions: dict[str, SemanticPythonRegion],
    python_region_executor: PythonRegionExecutor | None,
) -> object | None:
    arguments = tuple(values[value] for value in operation.operands)
    if operation.op == "argument":
        index = int(operation.attribute("index") or "-1")
        result: object = inputs[index]
    elif operation.op == "constant":
        if operation.literal is None:
            raise ValueError("verified constant lost its literal")
        result = operation.literal.value
    elif operation.op in _BINARY:
        result = _BINARY[operation.op](*arguments)
    elif operation.op in _COMPARE:
        result = _COMPARE[operation.op](*arguments)
    elif operation.op == "null.is_null":
        result = arguments[0] is None
    elif operation.op == "cast":
        result = _CAST[operation.result_type](arguments[0])
    elif operation.op == "select":
        result = arguments[1] if arguments[0] else arguments[2]
    elif operation.op == "field.load":
        target = arguments[0]
        field_id = operation.attribute("field_id")
        if isinstance(target, Mapping):
            result = target[field_id]
        else:
            result = getattr(target, str(field_id))
    elif operation.op == "tuple.make":
        result = tuple(arguments)
    elif operation.op == "list.make":
        result = list(arguments)
    elif operation.op == "modeled.call":
        result = _modeled_call(operation, arguments)
    elif operation.op == "python.region":
        if python_region_executor is None:
            raise ValueError("Python region requires a reference executor")
        region = python_regions[operation.python_region_id or ""]
        result = python_region_executor(region, arguments)
    elif operation.op in {"branch", "jump", "return"}:
        return None
    else:
        raise ValueError(f"unsupported semantic operation: {operation.op}")
    if operation.result_id is None:
        raise ValueError("verified value operation lost its result")
    values[operation.result_id] = result
    return result


def reference_execute_semantic(
    module: SemanticCoreModule,
    inputs: tuple[object, ...],
    *,
    python_region_executor: PythonRegionExecutor | None = None,
    max_steps: int = 100_000,
) -> object:
    verify_semantic_module(module)
    if len(inputs) != len(module.input_types):
        raise TypeError("reference interpreter input count mismatch")
    if max_steps <= 0:
        raise ValueError("reference interpreter step budget must be positive")

    blocks = {block.block_id: block for block in module.blocks}
    operations = {
        operation.operation_id: operation
        for operation in module.operations
    }
    python_regions = {
        region.region_id: region for region in module.python_regions
    }
    values: dict[str, object] = {}
    block_id = module.entry_block
    steps = 0
    while True:
        block = blocks[block_id]
        next_block: str | None = None
        for operation_id in block.operation_ids:
            steps += 1
            if steps > max_steps:
                raise RuntimeError("reference interpreter step budget exceeded")
            operation = operations[operation_id]
            if operation.op == "branch":
                condition = values[operation.operands[0]]
                next_block = operation.attribute(
                    "true_block" if condition else "false_block"
                )
                break
            if operation.op == "jump":
                next_block = operation.attribute("target_block")
                break
            if operation.op == "return":
                return values[operation.operands[0]]
            _execute_operation(
                operation,
                values=values,
                inputs=inputs,
                python_regions=python_regions,
                python_region_executor=python_region_executor,
            )
        if next_block is None:
            raise RuntimeError("verified block did not terminate")
        block_id = next_block
