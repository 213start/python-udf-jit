from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from python_udf_jit.compiler.bytecode_decoder import (
    DecodedBytecode,
    DecodedInstruction,
    ExceptionHandler,
    verify_decoded_bytecode,
)


CFG_VERSION = 1
MAX_BLOCKS = 65_536
MAX_EDGES = 262_144
MAX_STATE_SLOTS = 1_000_000


class CfgRejectCode(StrEnum):
    INVALID_CONTROL_FLOW = "invalid_control_flow"
    BACKWARD_EDGE = "unsupported_backward_edge"
    STACK_UNDERFLOW = "stack_underflow"
    STACK_OVERFLOW = "stack_overflow"
    STACK_IMBALANCE = "stack_imbalance"
    INVALID_LOCAL = "invalid_local"
    UNREACHABLE_BLOCK = "unreachable_block"


class CfgBuildError(ValueError):
    def __init__(self, code: CfgRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(code: CfgRejectCode, detail: str = "") -> None:
    raise CfgBuildError(code, detail)


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class IncomingValue:
    predecessor_block: str
    value_id: str

    def to_document(self) -> dict[str, str]:
        return {
            "predecessor_block": self.predecessor_block,
            "value_id": self.value_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "IncomingValue":
        expected = {"predecessor_block", "value_id"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid incoming value fields")
        if not all(isinstance(document[name], str) for name in expected):
            raise ValueError("invalid incoming value string")
        return cls(document["predecessor_block"], document["value_id"])


@dataclass(frozen=True)
class BlockParameter:
    value_id: str
    kind: str
    slot: int
    incoming: tuple[IncomingValue, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "incoming": [value.to_document() for value in self.incoming],
            "kind": self.kind,
            "slot": self.slot,
            "value_id": self.value_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "BlockParameter":
        expected = {"incoming", "kind", "slot", "value_id"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid block parameter fields")
        if (
            not isinstance(document["kind"], str)
            or not isinstance(document["value_id"], str)
            or type(document["slot"]) is not int
            or not isinstance(document["incoming"], list)
        ):
            raise ValueError("invalid block parameter value")
        return cls(
            document["value_id"],
            document["kind"],
            document["slot"],
            tuple(IncomingValue.from_document(value) for value in document["incoming"]),
        )


@dataclass(frozen=True)
class BasicBlock:
    block_id: str
    start_offset: int
    end_offset: int
    instruction_offsets: tuple[int, ...]
    entry_stack_depth: int
    parameters: tuple[BlockParameter, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "end_offset": self.end_offset,
            "entry_stack_depth": self.entry_stack_depth,
            "instruction_offsets": list(self.instruction_offsets),
            "parameters": [parameter.to_document() for parameter in self.parameters],
            "start_offset": self.start_offset,
        }

    @classmethod
    def from_document(cls, document: object) -> "BasicBlock":
        expected = {
            "block_id",
            "end_offset",
            "entry_stack_depth",
            "instruction_offsets",
            "parameters",
            "start_offset",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid basic block fields")
        if not isinstance(document["block_id"], str) or any(
            type(document[name]) is not int
            for name in ("end_offset", "entry_stack_depth", "start_offset")
        ):
            raise ValueError("invalid basic block scalar")
        offsets = document["instruction_offsets"]
        parameters = document["parameters"]
        if (
            not isinstance(offsets, list)
            or any(type(value) is not int for value in offsets)
            or not isinstance(parameters, list)
        ):
            raise ValueError("invalid basic block sequence")
        return cls(
            document["block_id"],
            document["start_offset"],
            document["end_offset"],
            tuple(offsets),
            document["entry_stack_depth"],
            tuple(BlockParameter.from_document(value) for value in parameters),
        )


@dataclass(frozen=True)
class ControlEdge:
    source_block: str
    target_block: str
    kind: str
    target_stack_depth: int
    handler_index: int | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "handler_index": self.handler_index,
            "kind": self.kind,
            "source_block": self.source_block,
            "target_block": self.target_block,
            "target_stack_depth": self.target_stack_depth,
        }

    @classmethod
    def from_document(cls, document: object) -> "ControlEdge":
        expected = {
            "handler_index",
            "kind",
            "source_block",
            "target_block",
            "target_stack_depth",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid control edge fields")
        if (
            not isinstance(document["kind"], str)
            or not isinstance(document["source_block"], str)
            or not isinstance(document["target_block"], str)
            or type(document["target_stack_depth"]) is not int
            or (
                document["handler_index"] is not None
                and type(document["handler_index"]) is not int
            )
        ):
            raise ValueError("invalid control edge value")
        return cls(
            document["source_block"],
            document["target_block"],
            document["kind"],
            document["target_stack_depth"],
            document["handler_index"],
        )


@dataclass(frozen=True)
class InstructionState:
    bytecode_offset: int
    stack_before: tuple[str, ...]
    stack_after: tuple[str, ...]
    locals_before: tuple[str | None, ...]
    locals_after: tuple[str | None, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "bytecode_offset": self.bytecode_offset,
            "locals_after": list(self.locals_after),
            "locals_before": list(self.locals_before),
            "stack_after": list(self.stack_after),
            "stack_before": list(self.stack_before),
        }

    @classmethod
    def from_document(cls, document: object) -> "InstructionState":
        expected = {
            "bytecode_offset",
            "locals_after",
            "locals_before",
            "stack_after",
            "stack_before",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid instruction state fields")
        if type(document["bytecode_offset"]) is not int:
            raise ValueError("invalid instruction state offset")
        for name in ("locals_after", "locals_before", "stack_after", "stack_before"):
            if not isinstance(document[name], list) or any(
                value is not None and not isinstance(value, str)
                for value in document[name]
            ):
                raise ValueError("invalid instruction state sequence")
        if any(value is None for value in (*document["stack_before"], *document["stack_after"])):
            raise ValueError("stack values cannot be null")
        return cls(
            document["bytecode_offset"],
            tuple(document["stack_before"]),
            tuple(document["stack_after"]),
            tuple(document["locals_before"]),
            tuple(document["locals_after"]),
        )


@dataclass(frozen=True)
class ControlFlowGraph:
    format_version: int
    entry_block: str
    blocks: tuple[BasicBlock, ...]
    edges: tuple[ControlEdge, ...]
    instruction_states: tuple[InstructionState, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_document() for block in self.blocks],
            "edges": [edge.to_document() for edge in self.edges],
            "entry_block": self.entry_block,
            "format_version": self.format_version,
            "instruction_states": [
                state.to_document() for state in self.instruction_states
            ],
        }

    def canonical_bytes(self, decoded: DecodedBytecode) -> bytes:
        verify_control_flow_graph(self, decoded)
        return _canonical_bytes(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "ControlFlowGraph":
        expected = {
            "blocks",
            "edges",
            "entry_block",
            "format_version",
            "instruction_states",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid CFG fields")
        if (
            type(document["format_version"]) is not int
            or not isinstance(document["entry_block"], str)
        ):
            raise ValueError("invalid CFG scalar")
        for name in ("blocks", "edges", "instruction_states"):
            if not isinstance(document[name], list):
                raise ValueError("invalid CFG sequence")
        return cls(
            document["format_version"],
            document["entry_block"],
            tuple(BasicBlock.from_document(value) for value in document["blocks"]),
            tuple(ControlEdge.from_document(value) for value in document["edges"]),
            tuple(
                InstructionState.from_document(value)
                for value in document["instruction_states"]
            ),
        )


@dataclass
class _MutableBlock:
    block_id: str
    start_offset: int
    end_offset: int
    instructions: tuple[DecodedInstruction, ...]
    entry_stack: tuple[str, ...] = ()
    entry_locals: tuple[str | None, ...] = ()
    exit_stack: tuple[str, ...] = ()
    exit_locals: tuple[str | None, ...] = ()
    parameters: tuple[BlockParameter, ...] = ()
    states: tuple[InstructionState, ...] = ()


@dataclass(frozen=True)
class _RawEdge:
    source_block: str
    target_block: str
    kind: str
    handler_index: int | None = None
    exception_stack_depth: int | None = None


def _next_instruction(
    instructions: tuple[DecodedInstruction, ...],
    index: int,
) -> int | None:
    return instructions[index + 1].offset if index + 1 < len(instructions) else None


def _block_id(index: int) -> str:
    return f"b{index:04d}"


def _make_blocks(decoded: DecodedBytecode) -> list[_MutableBlock]:
    instructions = decoded.instructions
    if (
        len(instructions) * max(1, len(decoded.exception_handlers))
        > MAX_STATE_SLOTS
    ):
        _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "exception_block_budget")
    leaders = {instructions[0].offset}
    terminal_operations = {
        "exception.raise",
        "exception.reraise",
        "return.constant",
        "return.value",
    }
    for index, instruction in enumerate(instructions):
        if instruction.jump_target is not None:
            leaders.add(instruction.jump_target)
            following = _next_instruction(instructions, index)
            if following is not None:
                leaders.add(following)
        elif instruction.operation in terminal_operations:
            following = _next_instruction(instructions, index)
            if following is not None:
                leaders.add(following)
    offsets = {instruction.offset for instruction in instructions}
    instruction_indexes = {
        instruction.offset: index
        for index, instruction in enumerate(instructions)
    }
    for handler in decoded.exception_handlers:
        for instruction in instructions:
            if handler.start_offset <= instruction.offset < handler.end_offset:
                leaders.add(instruction.offset)
                following = _next_instruction(
                    instructions,
                    instruction_indexes[instruction.offset],
                )
                if following is not None:
                    leaders.add(following)
        leaders.add(handler.target_offset)
        if handler.end_offset in offsets:
            leaders.add(handler.end_offset)
    ordered_leaders = sorted(leaders)
    if len(ordered_leaders) > MAX_BLOCKS:
        _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "block_budget")
    blocks: list[_MutableBlock] = []
    for index, start in enumerate(ordered_leaders):
        end = ordered_leaders[index + 1] if index + 1 < len(ordered_leaders) else decoded.code_size
        block_instructions = tuple(
            instruction
            for instruction in instructions
            if start <= instruction.offset < end
        )
        if not block_instructions or block_instructions[0].offset != start:
            _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "empty_block")
        blocks.append(_MutableBlock(_block_id(index), start, end, block_instructions))
    return blocks


def _normal_edge_kind(instruction: DecodedInstruction, *, target: bool) -> str:
    if instruction.operation == "branch.always":
        return "jump"
    if instruction.operation == "branch.if_false":
        return "branch_false" if target else "branch_true"
    if instruction.operation == "branch.if_true":
        return "branch_true" if target else "branch_false"
    if instruction.operation == "branch.if_none":
        return "branch_none" if target else "branch_not_none"
    if instruction.operation == "branch.if_not_none":
        return "branch_not_none" if target else "branch_none"
    return "fallthrough"


def _make_edges(
    decoded: DecodedBytecode,
    blocks: list[_MutableBlock],
) -> list[_RawEdge]:
    block_by_start = {block.start_offset: block for block in blocks}
    edges: list[_RawEdge] = []
    terminal_operations = {
        "exception.raise",
        "exception.reraise",
        "return.constant",
        "return.value",
    }
    if len(blocks) * max(1, len(decoded.exception_handlers)) > MAX_STATE_SLOTS:
        _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "exception_edge_budget")
    conditional_prefix = "branch.if_"
    for index, block in enumerate(blocks):
        last = block.instructions[-1]
        if last.jump_target is not None:
            target = block_by_start[last.jump_target]
            if target.start_offset <= block.start_offset:
                _fail(CfgRejectCode.BACKWARD_EDGE, str(last.offset))
            edges.append(
                _RawEdge(
                    block.block_id,
                    target.block_id,
                    _normal_edge_kind(last, target=True),
                )
            )
            if last.operation.startswith(conditional_prefix):
                if index + 1 >= len(blocks):
                    _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "missing_fallthrough")
                edges.append(
                    _RawEdge(
                        block.block_id,
                        blocks[index + 1].block_id,
                        _normal_edge_kind(last, target=False),
                    )
                )
                if len(edges) > MAX_EDGES:
                    _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "edge_budget")
        elif last.operation not in terminal_operations and index + 1 < len(blocks):
            edges.append(
                _RawEdge(block.block_id, blocks[index + 1].block_id, "fallthrough")
            )

    for handler_index, handler in enumerate(decoded.exception_handlers):
        target = block_by_start[handler.target_offset]
        target_depth = handler.stack_depth + 1 + int(handler.preserve_lasti)
        for block in blocks:
            if handler.start_offset <= block.start_offset < handler.end_offset:
                if target.start_offset <= block.start_offset:
                    _fail(CfgRejectCode.BACKWARD_EDGE, f"exception:{handler_index}")
                edges.append(
                    _RawEdge(
                        block.block_id,
                        target.block_id,
                        "exception",
                        handler_index,
                        target_depth,
                    )
                )
                if len(edges) > MAX_EDGES:
                    _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "edge_budget")
    unique = {
        (
            edge.source_block,
            edge.target_block,
            edge.kind,
            edge.handler_index,
            edge.exception_stack_depth,
        ): edge
        for edge in edges
    }
    return [unique[key] for key in sorted(unique)]


def _topological_blocks(
    blocks: list[_MutableBlock],
    edges: list[_RawEdge],
) -> list[_MutableBlock]:
    block_by_id = {block.block_id: block for block in blocks}
    indegree = {block.block_id: 0 for block in blocks}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        indegree[edge.target_block] += 1
        outgoing[edge.source_block].append(edge.target_block)
    ready = sorted(
        (block.block_id for block in blocks if indegree[block.block_id] == 0),
        key=lambda block_id: block_by_id[block_id].start_offset,
    )
    ordered: list[_MutableBlock] = []
    while ready:
        block_id = ready.pop(0)
        ordered.append(block_by_id[block_id])
        for target in sorted(
            outgoing[block_id],
            key=lambda value: block_by_id[value].start_offset,
        ):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda value: block_by_id[value].start_offset)
    if len(ordered) != len(blocks):
        _fail(CfgRejectCode.BACKWARD_EDGE, "cycle")
    return ordered


def _local_index(instruction: DecodedInstruction, local_count: int) -> int:
    argument = instruction.argument
    if argument is None or not 0 <= argument < local_count:
        _fail(CfgRejectCode.INVALID_LOCAL, str(instruction.offset))
    return argument


def _pop(stack: list[str], count: int, instruction: DecodedInstruction) -> list[str]:
    if count < 0 or len(stack) < count:
        _fail(CfgRejectCode.STACK_UNDERFLOW, str(instruction.offset))
    if count == 0:
        return []
    values = stack[-count:]
    del stack[-count:]
    return values


def _result_ids(instruction: DecodedInstruction, count: int) -> tuple[str, ...]:
    return tuple(f"v{instruction.offset:06x}.{index}" for index in range(count))


def _generic_stack_shape(instruction: DecodedInstruction) -> tuple[int, int]:
    effect = instruction.stack_effect
    if effect < 0:
        return -effect, 0
    return 0, effect


def _simulate_instruction(
    instruction: DecodedInstruction,
    stack: list[str],
    locals_state: list[str | None],
) -> None:
    operation = instruction.operation
    if operation == "local.load":
        index = _local_index(instruction, len(locals_state))
        value = locals_state[index]
        stack.append(value if value is not None else f"unbound.local.{index}")
        return
    if operation == "local.store":
        index = _local_index(instruction, len(locals_state))
        locals_state[index] = _pop(stack, 1, instruction)[0]
        return
    if operation == "local.delete":
        index = _local_index(instruction, len(locals_state))
        locals_state[index] = None
        return
    if operation in {"constant.load", "constant.small_int"}:
        stack.extend(_result_ids(instruction, 1))
        return
    if operation == "stack.pop":
        _pop(stack, 1, instruction)
        return
    if operation == "stack.copy":
        argument = instruction.argument
        if argument is None or argument <= 0 or argument > len(stack):
            _fail(CfgRejectCode.STACK_UNDERFLOW, str(instruction.offset))
        stack.append(stack[-argument])
        return
    if operation == "stack.swap":
        argument = instruction.argument
        if argument is None or argument <= 0 or argument > len(stack):
            _fail(CfgRejectCode.STACK_UNDERFLOW, str(instruction.offset))
        stack[-1], stack[-argument] = stack[-argument], stack[-1]
        return
    if operation.startswith(("binary.", "compare.")) or operation == "index.load":
        _pop(stack, 2, instruction)
        stack.extend(_result_ids(instruction, 1))
        return
    if operation in {"field.load", "method.load"}:
        _pop(stack, 1, instruction)
        stack.extend(
            _result_ids(
                instruction,
                instruction.stack_effect + 1,
            )
        )
        return
    if operation in {"convert.bool", "unary.not", "unary.negative", "unary.positive"}:
        _pop(stack, 1, instruction)
        stack.extend(_result_ids(instruction, 1))
        return
    if operation in {"aggregate.tuple", "aggregate.list"}:
        count = instruction.argument
        if count is None or count < 0:
            _fail(CfgRejectCode.INVALID_CONTROL_FLOW, str(instruction.offset))
        _pop(stack, count, instruction)
        stack.extend(_result_ids(instruction, 1))
        return
    if operation.startswith("branch.if_"):
        if instruction.opcode_name.startswith("POP_JUMP"):
            _pop(stack, 1, instruction)
        return
    if operation == "return.value":
        _pop(stack, 1, instruction)
        return
    if operation == "return.constant":
        return
    if operation == "exception.push":
        stack.extend(_result_ids(instruction, 1))
        return
    if operation == "exception.match":
        _pop(stack, 2, instruction)
        stack.extend(_result_ids(instruction, 2))
        return
    if operation == "exception.pop":
        _pop(stack, 1, instruction)
        return
    if operation in {"exception.raise", "exception.reraise"}:
        pop_count = 1 if operation == "exception.reraise" else (instruction.argument or 0)
        _pop(stack, pop_count, instruction)
        return
    if operation in {
        "branch.always",
        "control.nop",
        "control.not_taken",
        "control.resume",
    }:
        return
    if operation == "call.opaque":
        push_count = 1
        pop_count = push_count - instruction.stack_effect
        _pop(stack, pop_count, instruction)
        stack.extend(_result_ids(instruction, push_count))
        return

    pop_count, push_count = _generic_stack_shape(instruction)
    _pop(stack, pop_count, instruction)
    stack.extend(_result_ids(instruction, push_count))


def _incoming_stack(
    edge: _RawEdge,
    source: _MutableBlock,
    handlers: tuple[ExceptionHandler, ...],
) -> tuple[str, ...]:
    if edge.kind != "exception":
        return source.exit_stack
    assert edge.exception_stack_depth is not None
    assert edge.handler_index is not None
    handler = handlers[edge.handler_index]
    if len(source.entry_stack) < handler.stack_depth:
        _fail(CfgRejectCode.STACK_UNDERFLOW, f"exception:{source.block_id}")
    values = list(source.entry_stack[: handler.stack_depth])
    while len(values) < edge.exception_stack_depth:
        values.append(
            f"exc.{edge.source_block}.{edge.target_block}.{len(values)}"
        )
    return tuple(values)


def _merge_values(
    block: _MutableBlock,
    incoming: list[tuple[_RawEdge, tuple[str, ...], tuple[str | None, ...]]],
    *,
    stack_size: int,
) -> tuple[tuple[str, ...], tuple[str | None, ...], tuple[BlockParameter, ...]]:
    stack_lengths = {len(stack) for _, stack, _ in incoming}
    if len(stack_lengths) != 1:
        _fail(CfgRejectCode.STACK_IMBALANCE, block.block_id)
    entry_stack: list[str] = []
    parameters: list[BlockParameter] = []
    for slot in range(next(iter(stack_lengths))):
        values = tuple(
            IncomingValue(edge.source_block, stack[slot])
            for edge, stack, _ in incoming
        )
        distinct = {value.value_id for value in values}
        if len(distinct) == 1:
            entry_stack.append(values[0].value_id)
        else:
            value_id = f"p.{block.block_id}.stack.{slot}"
            entry_stack.append(value_id)
            parameters.append(BlockParameter(value_id, "stack", slot, values))

    local_count = len(incoming[0][2])
    entry_locals: list[str | None] = []
    for slot in range(local_count):
        raw_values = tuple(locals_state[slot] for _, _, locals_state in incoming)
        if all(value == raw_values[0] for value in raw_values):
            entry_locals.append(raw_values[0])
            continue
        values = tuple(
            IncomingValue(
                edge.source_block,
                value if value is not None else f"unbound.local.{slot}",
            )
            for (edge, _, _), value in zip(incoming, raw_values, strict=True)
        )
        value_id = f"p.{block.block_id}.local.{slot}"
        entry_locals.append(value_id)
        parameters.append(BlockParameter(value_id, "local", slot, values))
    if len(entry_stack) > stack_size:
        _fail(CfgRejectCode.STACK_OVERFLOW, block.block_id)
    return tuple(entry_stack), tuple(entry_locals), tuple(parameters)


def build_control_flow_graph(
    decoded: DecodedBytecode,
    *,
    _verify: bool = True,
) -> ControlFlowGraph:
    verify_decoded_bytecode(decoded)
    state_width = max(1, decoded.local_count * 2 + decoded.stack_size * 2)
    if len(decoded.instructions) * state_width > MAX_STATE_SLOTS:
        _fail(CfgRejectCode.INVALID_CONTROL_FLOW, "ssa_state_budget")
    blocks = _make_blocks(decoded)
    edges = _make_edges(decoded, blocks)
    ordered = _topological_blocks(blocks, edges)
    block_by_id = {block.block_id: block for block in blocks}
    incoming_edges: dict[str, list[_RawEdge]] = defaultdict(list)
    for edge in edges:
        incoming_edges[edge.target_block].append(edge)

    entry = blocks[0]
    initial_locals: tuple[str | None, ...] = tuple(
        f"arg.{index}" if index < decoded.argument_count else None
        for index in range(decoded.local_count)
    )
    for block in ordered:
        if block is entry:
            block.entry_stack = ()
            block.entry_locals = initial_locals
        else:
            incoming: list[
                tuple[_RawEdge, tuple[str, ...], tuple[str | None, ...]]
            ] = []
            for edge in sorted(
                incoming_edges[block.block_id],
                key=lambda value: (
                    block_by_id[value.source_block].start_offset,
                    value.kind,
                    -1 if value.handler_index is None else value.handler_index,
                ),
            ):
                source = block_by_id[edge.source_block]
                incoming.append(
                    (
                        edge,
                        _incoming_stack(
                            edge,
                            source,
                            decoded.exception_handlers,
                        ),
                        (
                            source.entry_locals
                            if edge.kind == "exception"
                            else source.exit_locals
                        ),
                    )
                )
            if not incoming:
                _fail(CfgRejectCode.UNREACHABLE_BLOCK, block.block_id)
            (
                block.entry_stack,
                block.entry_locals,
                block.parameters,
            ) = _merge_values(block, incoming, stack_size=decoded.stack_size)

        stack = list(block.entry_stack)
        locals_state = list(block.entry_locals)
        states: list[InstructionState] = []
        for instruction in block.instructions:
            stack_before = tuple(stack)
            locals_before = tuple(locals_state)
            _simulate_instruction(instruction, stack, locals_state)
            if len(stack) > decoded.stack_size:
                _fail(CfgRejectCode.STACK_OVERFLOW, str(instruction.offset))
            states.append(
                InstructionState(
                    instruction.offset,
                    stack_before,
                    tuple(stack),
                    locals_before,
                    tuple(locals_state),
                )
            )
        block.exit_stack = tuple(stack)
        block.exit_locals = tuple(locals_state)
        block.states = tuple(states)

    immutable_blocks = tuple(
        BasicBlock(
            block.block_id,
            block.start_offset,
            block.end_offset,
            tuple(instruction.offset for instruction in block.instructions),
            len(block.entry_stack),
            block.parameters,
        )
        for block in blocks
    )
    immutable_edges = tuple(
        ControlEdge(
            edge.source_block,
            edge.target_block,
            edge.kind,
            (
                edge.exception_stack_depth
                if edge.exception_stack_depth is not None
                else len(block_by_id[edge.source_block].exit_stack)
            ),
            edge.handler_index,
        )
        for edge in edges
    )
    states = tuple(
        state
        for block in blocks
        for state in block.states
    )
    result = ControlFlowGraph(
        CFG_VERSION,
        entry.block_id,
        immutable_blocks,
        immutable_edges,
        states,
    )
    if _verify:
        verify_control_flow_graph(result, decoded)
    return result


def verify_control_flow_graph(
    graph: ControlFlowGraph,
    decoded: DecodedBytecode,
) -> None:
    if graph.format_version != CFG_VERSION:
        raise ValueError("unsupported CFG version")
    if not graph.blocks or len(graph.blocks) > MAX_BLOCKS:
        raise ValueError("invalid CFG block count")
    if graph.entry_block != graph.blocks[0].block_id:
        raise ValueError("invalid CFG entry block")
    block_ids = [block.block_id for block in graph.blocks]
    if block_ids != [_block_id(index) for index in range(len(graph.blocks))]:
        raise ValueError("noncanonical CFG block ids")
    if any(
        edge.source_block not in block_ids
        or edge.target_block not in block_ids
        or edge.target_stack_depth < 0
        for edge in graph.edges
    ):
        raise ValueError("invalid CFG edge")
    expected = build_control_flow_graph(decoded, _verify=False)
    if graph != expected:
        raise ValueError("CFG does not match decoded bytecode")
