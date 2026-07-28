from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.core_ir import (
    LogicalType,
    SemanticBlock,
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


@dataclass(frozen=True)
class _SemanticIndex:
    blocks: dict[str, SemanticBlock]
    block_order: tuple[SemanticBlock, ...]
    operations: dict[str, SemanticOperation]
    python_regions: dict[str, SemanticPythonRegion]
    successors: dict[str, frozenset[str]]


@dataclass(frozen=True)
class _ResumePlan:
    region: SemanticPythonRegion
    block_id: str
    first_operation_index: int
    live_names: tuple[str, ...]


def _semantic_index(module: SemanticCoreModule) -> _SemanticIndex:
    successors: dict[str, set[str]] = {
        block.block_id: set() for block in module.blocks
    }
    for edge in module.control_edges:
        successors[edge.source_block].add(edge.target_block)
    return _SemanticIndex(
        blocks={block.block_id: block for block in module.blocks},
        block_order=module.blocks,
        operations={
            operation.operation_id: operation
            for operation in module.operations
        },
        python_regions={
            region.region_id: region for region in module.python_regions
        },
        successors={
            block_id: frozenset(targets)
            for block_id, targets in successors.items()
        },
    )


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


def _execute_from_block(
    index: _SemanticIndex,
    inputs: tuple[object, ...],
    *,
    values: dict[str, object],
    block_id: str,
    first_operation_index: int,
    python_region_executor: PythonRegionExecutor | None,
    max_steps: int,
) -> object:
    steps = 0
    operation_index = first_operation_index
    while True:
        block = index.blocks[block_id]
        next_block: str | None = None
        for operation_id in block.operation_ids[operation_index:]:
            steps += 1
            if steps > max_steps:
                raise RuntimeError(
                    "reference interpreter step budget exceeded"
                )
            operation = index.operations[operation_id]
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
                python_regions=index.python_regions,
                python_region_executor=python_region_executor,
            )
        if next_block is None:
            raise RuntimeError("verified block did not terminate")
        block_id = next_block
        operation_index = 0


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

    return _execute_from_block(
        _semantic_index(module),
        inputs,
        values={},
        block_id=module.entry_block,
        first_operation_index=0,
        python_region_executor=python_region_executor,
        max_steps=max_steps,
    )


def _resume_region(
    index: _SemanticIndex,
    resume_id: str,
) -> SemanticPythonRegion:
    if not isinstance(resume_id, str):
        raise TypeError("reference continuation resume id must be a string")
    prefix, separator, semantic_resume_id = resume_id.partition(":")
    if separator != ":" or prefix != "v1" or not semantic_resume_id:
        raise ValueError("reference continuation ABI mismatch")
    matches = tuple(
        region
        for region in index.python_regions.values()
        if region.resume_id == semantic_resume_id
    )
    if len(matches) != 1:
        raise ValueError("reference continuation resume id mismatch")
    return matches[0]


def _resume_plan(
    index: _SemanticIndex,
    resume_id: str,
) -> _ResumePlan:
    region = _resume_region(index, resume_id)
    operation = index.operations[region.operation_id]
    block = index.blocks[operation.block_id]
    start_index = block.operation_ids.index(operation.operation_id) + 1

    reachable = {block.block_id}
    pending = [block.block_id]
    while pending:
        current = pending.pop()
        for successor in index.successors[current]:
            if successor == block.block_id:
                raise ValueError(
                    "reference continuation cyclic resume unsupported"
                )
            if successor not in reachable:
                reachable.add(successor)
                pending.append(successor)

    suffix_operations: list[SemanticOperation] = []
    for candidate in index.block_order:
        if candidate.block_id not in reachable:
            continue
        operation_ids = candidate.operation_ids
        if candidate.block_id == block.block_id:
            operation_ids = operation_ids[start_index:]
        suffix_operations.extend(
            index.operations[operation_id]
            for operation_id in operation_ids
        )
    suffix_definitions = {
        candidate.result_id
        for candidate in suffix_operations
        if candidate.result_id is not None
    }
    live_names: list[str] = []
    seen_live_names: set[str] = set()
    for candidate in suffix_operations:
        for operand in candidate.operands:
            if (
                operand not in suffix_definitions
                and operand not in seen_live_names
            ):
                live_names.append(operand)
                seen_live_names.add(operand)
    if not set(region.live_out) <= set(live_names):
        raise ValueError("reference continuation live-out is incomplete")
    return _ResumePlan(
        region,
        block.block_id,
        start_index,
        tuple(live_names),
    )


def reference_resume_live_names(
    module: SemanticCoreModule,
    resume_id: str,
) -> tuple[str, ...]:
    """Return the exact SSA values crossing a verified Python region exit."""

    verify_semantic_module(module)
    return _resume_plan(
        _semantic_index(module),
        resume_id,
    ).live_names


def reference_resume_semantic(
    module: SemanticCoreModule,
    resume_id: str,
    live_values: Mapping[str, object],
    *,
    python_region_executor: PythonRegionExecutor | None = None,
    max_steps: int = 100_000,
) -> object:
    """Execute only the verified semantic suffix after one Python region."""

    verify_semantic_module(module)
    if not isinstance(live_values, Mapping):
        raise TypeError("reference continuation values must be a mapping")
    if max_steps <= 0:
        raise ValueError("reference interpreter step budget must be positive")
    index = _semantic_index(module)
    plan = _resume_plan(index, resume_id)
    if set(live_values) != set(plan.live_names):
        raise ValueError("reference continuation live values mismatch")

    return _execute_from_block(
        index,
        (),
        values=dict(live_values),
        block_id=plan.block_id,
        first_operation_index=plan.first_operation_index,
        python_region_executor=python_region_executor,
        max_steps=max_steps,
    )
