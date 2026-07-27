from __future__ import annotations

import hashlib
import json
import types
from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.call_models import (
    CallKind,
    Effect,
    classify_calls,
)
from python_udf_jit.compiler.capture_ir import (
    CaptureFrontend,
    build_capture_frontend,
)
from python_udf_jit.compiler.identity import (
    CaptureIdentities,
    capture_identities,
    verify_capture_identities,
)


ABSTRACT_CAPTURE_VERSION = 1


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _digest(document: object) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


@dataclass(frozen=True)
class AbstractOperation:
    bytecode_offset: int
    operation: str
    effect: Effect
    execution: str
    may_raise: bool
    call_kind: str | None
    name_sha256: str | None

    def to_document(self) -> dict[str, Any]:
        return {
            "bytecode_offset": self.bytecode_offset,
            "call_kind": self.call_kind,
            "effect": self.effect.value,
            "execution": self.execution,
            "may_raise": self.may_raise,
            "name_sha256": self.name_sha256,
            "operation": self.operation,
        }

    @classmethod
    def from_document(cls, document: object) -> "AbstractOperation":
        expected = {
            "bytecode_offset",
            "call_kind",
            "effect",
            "execution",
            "may_raise",
            "name_sha256",
            "operation",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid abstract operation fields")
        if (
            type(document["bytecode_offset"]) is not int
            or not isinstance(document["operation"], str)
            or not isinstance(document["execution"], str)
            or type(document["may_raise"]) is not bool
        ):
            raise ValueError("invalid abstract operation scalar")
        for name in ("call_kind", "name_sha256"):
            if document[name] is not None and not isinstance(
                document[name],
                str,
            ):
                raise ValueError("invalid abstract operation optional field")
        return cls(
            document["bytecode_offset"],
            document["operation"],
            Effect(document["effect"]),
            document["execution"],
            document["may_raise"],
            document["call_kind"],
            document["name_sha256"],
        )


@dataclass(frozen=True)
class ExceptionState:
    handler_offsets: tuple[int, ...]
    preserves_lasti: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "handler_offsets": list(self.handler_offsets),
            "preserves_lasti": self.preserves_lasti,
        }

    @classmethod
    def from_document(cls, document: object) -> "ExceptionState":
        expected = {"handler_offsets", "preserves_lasti"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid exception state fields")
        offsets = document["handler_offsets"]
        if (
            not isinstance(offsets, list)
            or any(type(value) is not int for value in offsets)
            or type(document["preserves_lasti"]) is not bool
        ):
            raise ValueError("invalid exception state")
        return cls(tuple(offsets), document["preserves_lasti"])


@dataclass(frozen=True)
class PythonRegion:
    start_offset: int
    end_offset: int
    instruction_offsets: tuple[int, ...]
    live_in: tuple[str, ...]
    live_out: tuple[str, ...]
    effect: Effect
    may_raise: bool
    resume_offset: int
    resume_id: str
    exception_state: ExceptionState

    def semantic_document(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "end_offset": self.end_offset,
            "exception_state": self.exception_state.to_document(),
            "instruction_offsets": list(self.instruction_offsets),
            "live_in": list(self.live_in),
            "live_out": list(self.live_out),
            "may_raise": self.may_raise,
            "resume_offset": self.resume_offset,
            "start_offset": self.start_offset,
        }

    def recompute_resume_id(self, code_sha256: str) -> str:
        return _digest(
            {
                "code_sha256": code_sha256,
                "region": self.semantic_document(),
            }
        )

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "resume_id": self.resume_id,
        }

    @classmethod
    def from_document(cls, document: object) -> "PythonRegion":
        expected = {
            "effect",
            "end_offset",
            "exception_state",
            "instruction_offsets",
            "live_in",
            "live_out",
            "may_raise",
            "resume_id",
            "resume_offset",
            "start_offset",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid Python region fields")
        for name in (
            "end_offset",
            "resume_offset",
            "start_offset",
        ):
            if type(document[name]) is not int:
                raise ValueError("invalid Python region offset")
        for name in ("instruction_offsets", "live_in", "live_out"):
            if not isinstance(document[name], list):
                raise ValueError("invalid Python region sequence")
        if any(
            type(value) is not int
            for value in document["instruction_offsets"]
        ) or any(
            not isinstance(value, str)
            for name in ("live_in", "live_out")
            for value in document[name]
        ):
            raise ValueError("invalid Python region sequence value")
        if (
            type(document["may_raise"]) is not bool
            or not isinstance(document["resume_id"], str)
        ):
            raise ValueError("invalid Python region scalar")
        return cls(
            document["start_offset"],
            document["end_offset"],
            tuple(document["instruction_offsets"]),
            tuple(document["live_in"]),
            tuple(document["live_out"]),
            Effect(document["effect"]),
            document["may_raise"],
            document["resume_offset"],
            document["resume_id"],
            ExceptionState.from_document(document["exception_state"]),
        )


@dataclass(frozen=True)
class AbstractCapture:
    format_version: int
    code_sha256: str
    operations: tuple[AbstractOperation, ...]
    python_regions: tuple[PythonRegion, ...]
    semantic_hash: str

    def semantic_document(self) -> dict[str, Any]:
        return {
            "code_sha256": self.code_sha256,
            "format_version": self.format_version,
            "operations": [
                operation.to_document() for operation in self.operations
            ],
            "python_regions": [
                region.to_document() for region in self.python_regions
            ],
        }

    def recompute_semantic_hash(self) -> str:
        return _digest(self.semantic_document())

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_document(cls, document: object) -> "AbstractCapture":
        expected = {
            "code_sha256",
            "format_version",
            "operations",
            "python_regions",
            "semantic_hash",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid abstract capture fields")
        if (
            type(document["format_version"]) is not int
            or not isinstance(document["code_sha256"], str)
            or not isinstance(document["semantic_hash"], str)
            or not isinstance(document["operations"], list)
            or not isinstance(document["python_regions"], list)
        ):
            raise ValueError("invalid abstract capture scalar")
        return cls(
            document["format_version"],
            document["code_sha256"],
            tuple(
                AbstractOperation.from_document(value)
                for value in document["operations"]
            ),
            tuple(
                PythonRegion.from_document(value)
                for value in document["python_regions"]
            ),
            document["semantic_hash"],
        )


@dataclass(frozen=True)
class CapturedProgram:
    frontend: CaptureFrontend
    analysis: AbstractCapture
    identities: CaptureIdentities

    def to_document(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis.to_document(),
            "frontend": self.frontend.to_document(),
            "identities": self.identities.to_document(),
        }

    def canonical_bytes(self) -> bytes:
        from python_udf_jit.compiler.capture_verifier import (
            verify_captured_program,
        )

        verify_captured_program(self)
        return _canonical_bytes(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "CapturedProgram":
        expected = {"analysis", "frontend", "identities"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid captured program fields")
        result = cls(
            CaptureFrontend.from_document(document["frontend"]),
            AbstractCapture.from_document(document["analysis"]),
            CaptureIdentities.from_document(document["identities"]),
        )
        from python_udf_jit.compiler.capture_verifier import (
            verify_captured_program,
        )

        verify_captured_program(result)
        return result


def _effect_for_operation(operation: str) -> tuple[Effect, bool]:
    if operation.startswith(("field.", "method.", "python.")):
        return Effect.SIDE_EFFECT, True
    if operation.startswith(
        (
            "binary.",
            "compare.",
            "convert.",
            "index.",
            "unary.",
        )
    ):
        return Effect.MAY_RAISE, True
    if operation.startswith(("aggregate.", "exception.", "global.", "python.")):
        return Effect.MAY_RAISE, True
    return Effect.PURE, False


def _abstract_operations(
    frontend: CaptureFrontend,
    function: types.FunctionType,
) -> tuple[AbstractOperation, ...]:
    call_models = classify_calls(frontend.decoded_bytecode, function)
    operations: list[AbstractOperation] = []
    string_values: set[str] = set()
    states = {
        state.bytecode_offset: state
        for state in frontend.control_flow_graph.instruction_states
    }
    for instruction in frontend.decoded_bytecode.instructions:
        state = states[instruction.offset]
        if instruction.constant_kind == "str":
            string_values.update(
                set(state.stack_after) - set(state.stack_before)
            )
        call = call_models.get(instruction.offset)
        if call is not None:
            operations.append(
                AbstractOperation(
                    instruction.offset,
                    instruction.operation,
                    call.effect,
                    "python_region",
                    call.may_raise,
                    call.kind.value,
                    call.name_sha256,
                )
            )
            continue
        effect, may_raise = _effect_for_operation(
            instruction.operation
        )
        uses_string = bool(set(state.stack_before) & string_values)
        python_region = (
            instruction.capability == "python_region"
            or instruction.operation.startswith(
                ("exception.", "field.", "global.", "method.", "python.")
            )
            or instruction.operation.startswith("aggregate.")
            or uses_string
        )
        operations.append(
            AbstractOperation(
                instruction.offset,
                instruction.operation,
                effect,
                "python_region" if python_region else "capture",
                may_raise,
                None,
                None,
            )
        )
        if uses_string:
            string_values.update(
                set(state.stack_after) - set(state.stack_before)
            )
    return tuple(operations)


def _region_indexes(
    frontend: CaptureFrontend,
    operations: tuple[AbstractOperation, ...],
) -> list[tuple[int, ...]]:
    instruction_indexes = {
        instruction.offset: index
        for index, instruction in enumerate(
            frontend.decoded_bytecode.instructions
        )
    }
    marked: set[int] = {
        instruction_indexes[operation.bytecode_offset]
        for operation in operations
        if operation.execution == "python_region"
    }
    for index in tuple(marked):
        operation = operations[index]
        if operation.call_kind is None:
            continue
        block = next(
            block
            for block in frontend.control_flow_graph.blocks
            if operation.bytecode_offset in block.instruction_offsets
        )
        block_indexes = [
            instruction_indexes[offset]
            for offset in block.instruction_offsets
        ]
        lower = min(block_indexes)
        cursor = index - 1
        while cursor >= lower:
            candidate = operations[cursor]
            if candidate.operation.startswith(
                (
                    "branch.",
                    "call.",
                    "return.",
                )
            ):
                break
            if candidate.operation in {
                "constant.load",
                "constant.small_int",
                "global.load",
                "local.load",
                "method.load",
                "stack.copy",
            }:
                marked.add(cursor)
                cursor -= 1
                continue
            break

    groups: list[tuple[int, ...]] = []
    for block in frontend.control_flow_graph.blocks:
        indexes = [
            instruction_indexes[offset]
            for offset in block.instruction_offsets
            if instruction_indexes[offset] in marked
        ]
        current: list[int] = []
        for index in indexes:
            if current and index != current[-1] + 1:
                groups.append(tuple(current))
                current = []
            current.append(index)
        if current:
            groups.append(tuple(current))
    return groups


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _build_regions(
    frontend: CaptureFrontend,
    operations: tuple[AbstractOperation, ...],
    code_sha256: str,
) -> tuple[PythonRegion, ...]:
    decoded = frontend.decoded_bytecode
    states = frontend.control_flow_graph.instruction_states
    state_by_offset = {
        state.bytecode_offset: state for state in states
    }
    all_values: set[str] = set()
    for state in states:
        all_values.update(state.stack_before)
        all_values.update(state.stack_after)
        all_values.update(
            value
            for value in (*state.locals_before, *state.locals_after)
            if value is not None
        )

    regions: list[PythonRegion] = []
    for indexes in _region_indexes(frontend, operations):
        selected = tuple(
            decoded.instructions[index] for index in indexes
        )
        first_state = state_by_offset[selected[0].offset]
        last_state = state_by_offset[selected[-1].offset]
        live_in = list(first_state.stack_before)
        for instruction in selected:
            if instruction.operation == "local.load":
                assert instruction.argument is not None
                value = state_by_offset[
                    instruction.offset
                ].locals_before[instruction.argument]
                if value is not None:
                    live_in.append(value)

        later_values: set[str] = set()
        for state in states:
            if state.bytecode_offset > selected[-1].offset:
                later_values.update(state.stack_before)
                later_values.update(
                    value
                    for value in state.locals_before
                    if value is not None
                )
        live_out = [
            value
            for value in (
                *last_state.stack_after,
                *(
                    after
                    for before, after in zip(
                        first_state.locals_before,
                        last_state.locals_after,
                        strict=True,
                    )
                    if after is not None and after != before
                ),
            )
            if value in later_values
        ]
        live_in_tuple = _unique(
            [value for value in live_in if value in all_values]
        )
        live_out_tuple = _unique(live_out)
        handler_offsets: set[int] = set()
        preserve_lasti = False
        for handler in decoded.exception_handlers:
            if any(
                handler.start_offset
                <= instruction.offset
                < handler.end_offset
                for instruction in selected
            ):
                handler_offsets.add(handler.target_offset)
                preserve_lasti = (
                    preserve_lasti or handler.preserve_lasti
                )
        selected_operations = [operations[index] for index in indexes]
        effect = (
            Effect.SIDE_EFFECT
            if any(
                operation.effect is Effect.SIDE_EFFECT
                for operation in selected_operations
            )
            else Effect.MAY_RAISE
            if any(
                operation.effect is Effect.MAY_RAISE
                for operation in selected_operations
            )
            else Effect.PURE
        )
        next_index = indexes[-1] + 1
        resume_offset = (
            decoded.instructions[next_index].offset
            if next_index < len(decoded.instructions)
            else decoded.code_size
        )
        end_offset = resume_offset
        provisional = PythonRegion(
            selected[0].offset,
            end_offset,
            tuple(instruction.offset for instruction in selected),
            live_in_tuple,
            live_out_tuple,
            effect,
            any(
                operation.may_raise
                for operation in selected_operations
            ),
            resume_offset,
            "",
            ExceptionState(
                tuple(sorted(handler_offsets)),
                preserve_lasti,
            ),
        )
        regions.append(
            PythonRegion(
                **{
                    **provisional.__dict__,
                    "resume_id": provisional.recompute_resume_id(
                        code_sha256
                    ),
                }
            )
        )
    return tuple(regions)


def analyze_function(
    function: types.FunctionType,
    *,
    namespace_salt: str = "python-udf-jit",
    identities: CaptureIdentities | None = None,
) -> CapturedProgram:
    if type(function) is not types.FunctionType:
        raise TypeError("capture analysis requires an exact function")
    frontend = build_capture_frontend(function.__code__)
    if identities is None:
        identities = capture_identities(
            function,
            namespace_salt=namespace_salt,
        )
    else:
        verify_capture_identities(identities)
    operations = _abstract_operations(frontend, function)
    regions = _build_regions(
        frontend,
        operations,
        identities.code.sha256,
    )
    provisional = AbstractCapture(
        ABSTRACT_CAPTURE_VERSION,
        identities.code.sha256,
        operations,
        regions,
        "",
    )
    analysis = AbstractCapture(
        provisional.format_version,
        provisional.code_sha256,
        provisional.operations,
        provisional.python_regions,
        provisional.recompute_semantic_hash(),
    )
    result = CapturedProgram(frontend, analysis, identities)
    from python_udf_jit.compiler.capture_verifier import (
        verify_captured_program,
    )

    verify_captured_program(result)
    return result
