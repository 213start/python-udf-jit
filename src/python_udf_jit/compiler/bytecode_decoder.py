from __future__ import annotations

import dis
import json
import platform
import sys
import sysconfig
import types
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from python_udf_jit.compiler.source_map import (
    SourceMap,
    SourceMapError,
    decode_source_map,
    verify_source_map,
)


DECODED_BYTECODE_VERSION = 1
SUPPORTED_MAJOR_MINOR = (3, 14)
MAX_CODE_BYTES = 1 << 20
MAX_INSTRUCTIONS = 65_536
MAX_EXCEPTION_ENTRIES = 16_384


class DecodeRejectCode(StrEnum):
    UNSUPPORTED_FORMAT = "unsupported_bytecode_format"
    INVALID_BYTECODE = "invalid_bytecode"
    UNKNOWN_OPCODE = "unknown_opcode"
    INVALID_ARGUMENT = "invalid_opcode_argument"
    INVALID_JUMP = "invalid_jump_target"
    INVALID_EXCEPTION_TABLE = "invalid_exception_table"
    INVALID_LOCATION_TABLE = "invalid_location_table"
    BUDGET_EXCEEDED = "bytecode_budget_exceeded"


class BytecodeDecodeError(ValueError):
    def __init__(self, code: DecodeRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(code: DecodeRejectCode, detail: str = "") -> None:
    raise BytecodeDecodeError(code, detail)


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class BytecodeFormat:
    format_version: int
    implementation: str
    major: int
    minor: int
    cache_tag: str
    soabi_family: str
    decoder_id: str

    def to_document(self) -> dict[str, Any]:
        return {
            "cache_tag": self.cache_tag,
            "decoder_id": self.decoder_id,
            "format_version": self.format_version,
            "implementation": self.implementation,
            "major": self.major,
            "minor": self.minor,
            "soabi_family": self.soabi_family,
        }

    @classmethod
    def from_document(cls, document: object) -> "BytecodeFormat":
        expected = {
            "cache_tag",
            "decoder_id",
            "format_version",
            "implementation",
            "major",
            "minor",
            "soabi_family",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid bytecode format fields")
        if any(
            type(document[name]) is not int
            for name in ("format_version", "major", "minor")
        ):
            raise ValueError("invalid bytecode format number")
        if any(
            not isinstance(document[name], str)
            for name in ("cache_tag", "decoder_id", "implementation", "soabi_family")
        ):
            raise ValueError("invalid bytecode format string")
        result = cls(
            document["format_version"],
            document["implementation"],
            document["major"],
            document["minor"],
            document["cache_tag"],
            document["soabi_family"],
            document["decoder_id"],
        )
        verify_bytecode_format(result)
        return result


@dataclass(frozen=True)
class DecodedInstruction:
    offset: int
    start_offset: int
    opcode: int
    opcode_name: str
    operation: str
    argument: int | None
    jump_target: int | None
    stack_effect: int
    fallthrough_stack_effect: int
    jump_stack_effect: int
    capability: str
    constant_kind: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "argument": self.argument,
            "capability": self.capability,
            "constant_kind": self.constant_kind,
            "fallthrough_stack_effect": self.fallthrough_stack_effect,
            "jump_stack_effect": self.jump_stack_effect,
            "jump_target": self.jump_target,
            "offset": self.offset,
            "opcode": self.opcode,
            "opcode_name": self.opcode_name,
            "operation": self.operation,
            "stack_effect": self.stack_effect,
            "start_offset": self.start_offset,
        }

    @classmethod
    def from_document(cls, document: object) -> "DecodedInstruction":
        expected = {
            "argument",
            "capability",
            "constant_kind",
            "fallthrough_stack_effect",
            "jump_stack_effect",
            "jump_target",
            "offset",
            "opcode",
            "opcode_name",
            "operation",
            "stack_effect",
            "start_offset",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid decoded instruction fields")
        integer_fields = (
            "fallthrough_stack_effect",
            "jump_stack_effect",
            "offset",
            "opcode",
            "stack_effect",
            "start_offset",
        )
        if any(type(document[name]) is not int for name in integer_fields):
            raise ValueError("invalid decoded instruction number")
        for name in ("argument", "jump_target"):
            if document[name] is not None and type(document[name]) is not int:
                raise ValueError("invalid optional instruction number")
        if any(
            not isinstance(document[name], str)
            for name in ("capability", "opcode_name", "operation")
        ):
            raise ValueError("invalid decoded instruction string")
        if document["constant_kind"] is not None and not isinstance(
            document["constant_kind"], str
        ):
            raise ValueError("invalid constant kind")
        return cls(
            document["offset"],
            document["start_offset"],
            document["opcode"],
            document["opcode_name"],
            document["operation"],
            document["argument"],
            document["jump_target"],
            document["stack_effect"],
            document["fallthrough_stack_effect"],
            document["jump_stack_effect"],
            document["capability"],
            document["constant_kind"],
        )


@dataclass(frozen=True)
class ExceptionHandler:
    start_offset: int
    end_offset: int
    target_offset: int
    stack_depth: int
    preserve_lasti: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "end_offset": self.end_offset,
            "preserve_lasti": self.preserve_lasti,
            "stack_depth": self.stack_depth,
            "start_offset": self.start_offset,
            "target_offset": self.target_offset,
        }

    @classmethod
    def from_document(cls, document: object) -> "ExceptionHandler":
        expected = {
            "end_offset",
            "preserve_lasti",
            "stack_depth",
            "start_offset",
            "target_offset",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid exception handler fields")
        if any(
            type(document[name]) is not int
            for name in ("end_offset", "stack_depth", "start_offset", "target_offset")
        ) or type(document["preserve_lasti"]) is not bool:
            raise ValueError("invalid exception handler value")
        return cls(
            document["start_offset"],
            document["end_offset"],
            document["target_offset"],
            document["stack_depth"],
            document["preserve_lasti"],
        )


@dataclass(frozen=True)
class DecodedBytecode:
    format_version: int
    bytecode_format: BytecodeFormat
    code_size: int
    stack_size: int
    local_count: int
    argument_count: int
    instructions: tuple[DecodedInstruction, ...]
    exception_handlers: tuple[ExceptionHandler, ...]
    source_map: SourceMap

    def to_document(self) -> dict[str, Any]:
        return {
            "bytecode_format": self.bytecode_format.to_document(),
            "argument_count": self.argument_count,
            "code_size": self.code_size,
            "exception_handlers": [
                handler.to_document() for handler in self.exception_handlers
            ],
            "format_version": self.format_version,
            "instructions": [
                instruction.to_document() for instruction in self.instructions
            ],
            "local_count": self.local_count,
            "source_map": self.source_map.to_document(),
            "stack_size": self.stack_size,
        }

    def canonical_bytes(self) -> bytes:
        verify_decoded_bytecode(self)
        return _canonical_bytes(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "DecodedBytecode":
        expected = {
            "bytecode_format",
            "argument_count",
            "code_size",
            "exception_handlers",
            "format_version",
            "instructions",
            "local_count",
            "source_map",
            "stack_size",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid decoded bytecode fields")
        if any(
            type(document[name]) is not int
            for name in (
                "argument_count",
                "code_size",
                "format_version",
                "local_count",
                "stack_size",
            )
        ):
            raise ValueError("invalid decoded bytecode number")
        instructions = document["instructions"]
        handlers = document["exception_handlers"]
        if not isinstance(instructions, list) or not isinstance(handlers, list):
            raise ValueError("invalid decoded bytecode sequence")
        result = cls(
            document["format_version"],
            BytecodeFormat.from_document(document["bytecode_format"]),
            document["code_size"],
            document["stack_size"],
            document["local_count"],
            document["argument_count"],
            tuple(DecodedInstruction.from_document(value) for value in instructions),
            tuple(ExceptionHandler.from_document(value) for value in handlers),
            SourceMap.from_document(document["source_map"]),
        )
        verify_decoded_bytecode(result)
        return result


def verify_bytecode_format(bytecode_format: BytecodeFormat) -> None:
    expected = BytecodeFormat(
        DECODED_BYTECODE_VERSION,
        "cpython",
        3,
        14,
        "cpython-314",
        "cpython-314",
        "cpython-3.14-wordcode-v1",
    )
    if bytecode_format != expected:
        raise ValueError("unsupported bytecode format")


def detect_bytecode_format() -> BytecodeFormat:
    cache_tag = sys.implementation.cache_tag
    soabi = sysconfig.get_config_var("SOABI")
    if (
        platform.python_implementation() != "CPython"
        or sys.version_info[:2] != SUPPORTED_MAJOR_MINOR
        or cache_tag != "cpython-314"
        or not isinstance(soabi, str)
        or not soabi.startswith("cpython-314-")
    ):
        _fail(
            DecodeRejectCode.UNSUPPORTED_FORMAT,
            (
                f"{platform.python_implementation()}-"
                f"{sys.version_info.major}.{sys.version_info.minor}"
            ),
        )
    return BytecodeFormat(
        DECODED_BYTECODE_VERSION,
        "cpython",
        3,
        14,
        cache_tag,
        "cpython-314",
        "cpython-3.14-wordcode-v1",
    )


def _constant_kind(value: object) -> str:
    value_type = type(value)
    if value is None:
        return "none"
    if value is Ellipsis:
        return "ellipsis"
    if value_type is bool:
        return "bool"
    if value_type is int:
        return "int"
    if value_type is float:
        return "float"
    if value_type is complex:
        return "complex"
    if value_type is str:
        return "str"
    if value_type is bytes:
        return "bytes"
    if value_type is tuple:
        return "tuple"
    if value_type is frozenset:
        return "frozenset"
    if value_type is types.CodeType:
        return "code"
    return "opaque"


_DIRECT_OPERATIONS = {
    "RESUME": "control.resume",
    "NOP": "control.nop",
    "NOT_TAKEN": "control.not_taken",
    "LOAD_FAST": "local.load",
    "LOAD_FAST_BORROW": "local.load",
    "LOAD_FAST_CHECK": "local.load",
    "STORE_FAST": "local.store",
    "DELETE_FAST": "local.delete",
    "LOAD_CONST": "constant.load",
    "LOAD_SMALL_INT": "constant.small_int",
    "POP_TOP": "stack.pop",
    "COPY": "stack.copy",
    "SWAP": "stack.swap",
    "TO_BOOL": "convert.bool",
    "UNARY_NOT": "unary.not",
    "UNARY_NEGATIVE": "unary.negative",
    "UNARY_POSITIVE": "unary.positive",
    "BUILD_TUPLE": "aggregate.tuple",
    "BUILD_LIST": "aggregate.list",
    "RETURN_VALUE": "return.value",
    "RETURN_CONST": "return.constant",
    "PUSH_EXC_INFO": "exception.push",
    "CHECK_EXC_MATCH": "exception.match",
    "POP_EXCEPT": "exception.pop",
    "RERAISE": "exception.reraise",
    "RAISE_VARARGS": "exception.raise",
    "LOAD_GLOBAL": "global.load",
    "LOAD_NAME": "global.load",
    "CALL": "call.opaque",
    "CALL_KW": "call.opaque",
    "CALL_FUNCTION_EX": "call.opaque",
}


def _normal_operation(opname: str, argument: int | None) -> str:
    if opname == "LOAD_ATTR":
        if argument is None:
            _fail(DecodeRejectCode.INVALID_ARGUMENT, "LOAD_ATTR")
        return "method.load" if argument & 1 else "field.load"
    if opname == "BINARY_OP":
        if argument is None or not 0 <= argument < len(dis._nb_ops):
            _fail(DecodeRejectCode.INVALID_ARGUMENT, "BINARY_OP")
        symbol = dis._nb_ops[argument][1]
        names = {
            "+": "binary.add",
            "-": "binary.subtract",
            "*": "binary.multiply",
            "/": "binary.true_divide",
            "//": "binary.floor_divide",
            "%": "binary.remainder",
            "**": "binary.power",
            "[]": "index.load",
        }
        return names.get(symbol, "binary.other")
    if opname == "COMPARE_OP":
        if argument is None:
            _fail(DecodeRejectCode.INVALID_ARGUMENT, "COMPARE_OP")
        comparison_index = argument & 0xF
        if comparison_index >= len(dis.cmp_op):
            _fail(DecodeRejectCode.INVALID_ARGUMENT, "COMPARE_OP")
        return {
            "<": "compare.less",
            "<=": "compare.less_equal",
            "==": "compare.equal",
            "!=": "compare.not_equal",
            ">": "compare.greater",
            ">=": "compare.greater_equal",
        }[dis.cmp_op[comparison_index]]
    if opname in {"IS_OP", "CONTAINS_OP"}:
        return "compare.identity" if opname == "IS_OP" else "compare.contains"
    if opname in {"JUMP_FORWARD", "JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT"}:
        return "branch.always"
    if opname in {"POP_JUMP_IF_FALSE", "JUMP_IF_FALSE"}:
        return "branch.if_false"
    if opname in {"POP_JUMP_IF_TRUE", "JUMP_IF_TRUE"}:
        return "branch.if_true"
    if opname == "POP_JUMP_IF_NONE":
        return "branch.if_none"
    if opname == "POP_JUMP_IF_NOT_NONE":
        return "branch.if_not_none"
    return _DIRECT_OPERATIONS.get(opname, "python.opaque")


def _capability(operation: str, constant_kind: str | None) -> str:
    if (
        operation.startswith(("aggregate.", "call.", "global.", "python.", "exception."))
        or operation.startswith("method.")
        or constant_kind in {"str", "bytes", "tuple", "frozenset", "code", "opaque"}
    ):
        return "python_region"
    return "capture"


def _read_logical_instructions(code_bytes: bytes) -> list[tuple[int, int, int, str, int, int]]:
    if not code_bytes or len(code_bytes) % 2:
        _fail(DecodeRejectCode.INVALID_BYTECODE, "wordcode_alignment")
    if len(code_bytes) > MAX_CODE_BYTES:
        _fail(DecodeRejectCode.BUDGET_EXCEEDED, "code_bytes")

    physical: list[tuple[int, int, str, int, int]] = []
    unit = 0
    unit_count = len(code_bytes) // 2
    while unit < unit_count:
        offset = unit * 2
        opcode = code_bytes[offset]
        argument_byte = code_bytes[offset + 1]
        if opcode >= len(dis.opname):
            _fail(DecodeRejectCode.UNKNOWN_OPCODE, str(opcode))
        opname = dis.opname[opcode]
        if opname.startswith("<") or opname == "CACHE":
            _fail(DecodeRejectCode.UNKNOWN_OPCODE, str(opcode))
        cache_count = dis._inline_cache_entries.get(opname, 0)
        if type(cache_count) is not int or cache_count < 0:
            _fail(DecodeRejectCode.UNSUPPORTED_FORMAT, "inline_cache_table")
        if unit + cache_count >= unit_count:
            _fail(DecodeRejectCode.INVALID_BYTECODE, "truncated_inline_cache")
        for cache_unit in range(unit + 1, unit + cache_count + 1):
            if code_bytes[cache_unit * 2] != dis.opmap["CACHE"]:
                _fail(DecodeRejectCode.INVALID_BYTECODE, "invalid_inline_cache")
        physical.append((offset, opcode, opname, argument_byte, cache_count))
        unit += cache_count + 1
        if len(physical) > MAX_INSTRUCTIONS:
            _fail(DecodeRejectCode.BUDGET_EXCEEDED, "instructions")

    logical: list[tuple[int, int, int, str, int, int]] = []
    extended = 0
    extended_start: int | None = None
    extended_count = 0
    for offset, opcode, opname, argument_byte, cache_count in physical:
        has_argument = opcode in dis.hasarg
        if has_argument:
            argument = argument_byte | extended
        else:
            argument = 0
            if extended_start is not None:
                _fail(DecodeRejectCode.INVALID_BYTECODE, "extended_arg_without_argument")
        if opname == "EXTENDED_ARG":
            if not has_argument or cache_count:
                _fail(DecodeRejectCode.UNSUPPORTED_FORMAT, "extended_arg_contract")
            extended_count += 1
            if extended_count > 3:
                _fail(DecodeRejectCode.INVALID_ARGUMENT, "extended_arg_overflow")
            if extended_start is None:
                extended_start = offset
            extended = argument << 8
            if extended >= 1 << 31:
                extended -= 1 << 32
            continue
        logical.append(
            (
                offset,
                offset if extended_start is None else extended_start,
                opcode,
                opname,
                argument if has_argument else -1,
                cache_count,
            )
        )
        extended = 0
        extended_start = None
        extended_count = 0
    if extended_start is not None:
        _fail(DecodeRejectCode.INVALID_BYTECODE, "dangling_extended_arg")
    return logical


def _parse_varint(data: bytes, cursor: int, *, entry_start: bool) -> tuple[int, int]:
    if cursor >= len(data):
        _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "truncated_varint")
    first = data[cursor]
    if entry_start and not first & 0x80:
        _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "missing_entry_marker")
    if not entry_start and first & 0x80:
        _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "unexpected_entry_marker")
    value = first & 0x3F
    cursor += 1
    continuation = bool(first & 0x40)
    count = 1
    while continuation:
        if cursor >= len(data):
            _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "truncated_varint")
        byte = data[cursor]
        if byte & 0x80:
            _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "marker_inside_varint")
        value = (value << 6) | (byte & 0x3F)
        continuation = bool(byte & 0x40)
        cursor += 1
        count += 1
        if count > 6:
            _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "varint_overflow")
    return value, cursor


def _decode_exception_handlers(
    code: types.CodeType,
    instruction_offsets: set[int],
) -> tuple[ExceptionHandler, ...]:
    data = code.co_exceptiontable
    cursor = 0
    handlers: list[ExceptionHandler] = []
    previous_start = -1
    while cursor < len(data):
        start_units, cursor = _parse_varint(data, cursor, entry_start=True)
        length_units, cursor = _parse_varint(data, cursor, entry_start=False)
        target_units, cursor = _parse_varint(data, cursor, entry_start=False)
        depth_lasti, cursor = _parse_varint(data, cursor, entry_start=False)
        start = start_units * 2
        end = start + length_units * 2
        target = target_units * 2
        depth = depth_lasti >> 1
        preserve_lasti = bool(depth_lasti & 1)
        if (
            length_units <= 0
            or start < previous_start
            or start not in instruction_offsets
            or (end not in instruction_offsets and end != len(code.co_code))
            or target not in instruction_offsets
            or not 0 <= depth <= code.co_stacksize
        ):
            _fail(DecodeRejectCode.INVALID_EXCEPTION_TABLE, "invalid_entry")
        handlers.append(ExceptionHandler(start, end, target, depth, preserve_lasti))
        previous_start = start
        if len(handlers) > MAX_EXCEPTION_ENTRIES:
            _fail(DecodeRejectCode.BUDGET_EXCEEDED, "exception_handlers")
    return tuple(handlers)


def _stack_effect(opcode: int, argument: int | None, jump: bool | None = None) -> int:
    try:
        return dis.stack_effect(opcode, argument, jump=jump)
    except (ValueError, TypeError) as error:
        _fail(DecodeRejectCode.INVALID_ARGUMENT, dis.opname[opcode])
        raise AssertionError from error


def decode_code(code: types.CodeType) -> DecodedBytecode:
    """Decode one CPython 3.14 code object without formatting user values."""

    if type(code) is not types.CodeType:
        _fail(DecodeRejectCode.INVALID_BYTECODE, "not_exact_code")
    bytecode_format = detect_bytecode_format()
    logical = _read_logical_instructions(code.co_code)
    offsets = {instruction[0] for instruction in logical}
    decoded: list[DecodedInstruction] = []
    for offset, start_offset, opcode, opname, raw_argument, cache_count in logical:
        argument = raw_argument if opcode in dis.hasarg else None
        constant_kind = None
        if opname == "LOAD_CONST":
            if argument is None or not 0 <= argument < len(code.co_consts):
                _fail(DecodeRejectCode.INVALID_ARGUMENT, "LOAD_CONST")
            constant_kind = _constant_kind(code.co_consts[argument])
        jump_target = None
        if opcode in dis.hasjabs or opcode in dis.hasjrel:
            if argument is None:
                _fail(DecodeRejectCode.INVALID_ARGUMENT, opname)
            if opcode in dis.hasjabs:
                jump_target = argument * 2
            else:
                direction = -1 if "BACKWARD" in opname else 1
                jump_target = offset + 2 + cache_count * 2 + direction * argument * 2
            if jump_target not in offsets:
                _fail(DecodeRejectCode.INVALID_JUMP, str(jump_target))
        operation = _normal_operation(opname, argument)
        decoded.append(
            DecodedInstruction(
                offset,
                start_offset,
                opcode,
                opname,
                operation,
                argument,
                jump_target,
                _stack_effect(opcode, argument),
                _stack_effect(opcode, argument, False),
                _stack_effect(opcode, argument, True),
                _capability(operation, constant_kind),
                constant_kind,
            )
        )

    try:
        source_map = decode_source_map(code, (instruction.offset for instruction in decoded))
    except SourceMapError as error:
        _fail(DecodeRejectCode.INVALID_LOCATION_TABLE, str(error))
    handlers = _decode_exception_handlers(code, offsets)
    result = DecodedBytecode(
        DECODED_BYTECODE_VERSION,
        bytecode_format,
        len(code.co_code),
        code.co_stacksize,
        code.co_nlocals,
        code.co_argcount + code.co_kwonlyargcount,
        tuple(decoded),
        handlers,
        source_map,
    )
    try:
        verify_decoded_bytecode(result)
    except ValueError as error:
        _fail(DecodeRejectCode.INVALID_BYTECODE, str(error))
    return result


def verify_decoded_bytecode(decoded: DecodedBytecode) -> None:
    if decoded.format_version != DECODED_BYTECODE_VERSION:
        raise ValueError("unsupported decoded bytecode version")
    verify_bytecode_format(decoded.bytecode_format)
    if (
        type(decoded.code_size) is not int
        or decoded.code_size <= 0
        or decoded.code_size % 2
        or decoded.code_size > MAX_CODE_BYTES
        or type(decoded.stack_size) is not int
        or decoded.stack_size < 0
        or type(decoded.local_count) is not int
        or decoded.local_count < 0
        or type(decoded.argument_count) is not int
        or not 0 <= decoded.argument_count <= decoded.local_count
        or not decoded.instructions
        or len(decoded.instructions) > MAX_INSTRUCTIONS
    ):
        raise ValueError("invalid decoded bytecode bounds")
    offsets: list[int] = []
    allowed_constant_kinds = {
        "bool",
        "bytes",
        "code",
        "complex",
        "ellipsis",
        "float",
        "frozenset",
        "int",
        "none",
        "opaque",
        "str",
        "tuple",
    }
    for instruction in decoded.instructions:
        if (
            type(instruction.offset) is not int
            or type(instruction.start_offset) is not int
            or instruction.offset < 0
            or instruction.offset % 2
            or instruction.start_offset < 0
            or instruction.start_offset > instruction.offset
            or instruction.opcode < 0
            or instruction.opcode > 255
            or instruction.opcode >= len(dis.opname)
            or dis.opname[instruction.opcode].startswith("<")
            or instruction.opcode_name != dis.opname[instruction.opcode]
            or not instruction.opcode_name
            or not instruction.operation
            or instruction.capability not in {"capture", "python_region"}
        ):
            raise ValueError("invalid decoded instruction")
        expected_argument = instruction.opcode in dis.hasarg
        if expected_argument != (instruction.argument is not None):
            raise ValueError("invalid decoded instruction argument")
        try:
            expected_operation = _normal_operation(
                instruction.opcode_name,
                instruction.argument,
            )
            expected_effect = _stack_effect(
                instruction.opcode,
                instruction.argument,
            )
            expected_fallthrough_effect = _stack_effect(
                instruction.opcode,
                instruction.argument,
                False,
            )
            expected_jump_effect = _stack_effect(
                instruction.opcode,
                instruction.argument,
                True,
            )
        except BytecodeDecodeError as error:
            raise ValueError("invalid decoded instruction contract") from error
        if (
            instruction.operation != expected_operation
            or instruction.stack_effect != expected_effect
            or instruction.fallthrough_stack_effect != expected_fallthrough_effect
            or instruction.jump_stack_effect != expected_jump_effect
            or instruction.capability
            != _capability(expected_operation, instruction.constant_kind)
        ):
            raise ValueError("decoded instruction contract mismatch")
        if instruction.opcode_name == "LOAD_CONST":
            if instruction.constant_kind not in allowed_constant_kinds:
                raise ValueError("invalid decoded constant kind")
        elif instruction.constant_kind is not None:
            raise ValueError("constant kind on non-constant instruction")
        if instruction.jump_target is not None and instruction.jump_target < 0:
            raise ValueError("invalid jump target")
        if instruction.jump_target is not None:
            assert instruction.argument is not None
            cache_count = dis._inline_cache_entries.get(
                instruction.opcode_name,
                0,
            )
            if instruction.opcode in dis.hasjabs:
                expected_target = instruction.argument * 2
            elif instruction.opcode in dis.hasjrel:
                direction = (
                    -1 if "BACKWARD" in instruction.opcode_name else 1
                )
                expected_target = (
                    instruction.offset
                    + 2
                    + cache_count * 2
                    + direction * instruction.argument * 2
                )
            else:
                raise ValueError("jump target on non-jump instruction")
            if instruction.jump_target != expected_target:
                raise ValueError("jump target contract mismatch")
        elif instruction.opcode in dis.hasjabs or instruction.opcode in dis.hasjrel:
            raise ValueError("missing jump target")
        cache_count = dis._inline_cache_entries.get(
            instruction.opcode_name,
            0,
        )
        if instruction.offset + 2 + cache_count * 2 > decoded.code_size:
            raise ValueError("instruction exceeds code bounds")
        offsets.append(instruction.offset)
    if offsets != sorted(set(offsets)):
        raise ValueError("instruction offsets must be unique and sorted")
    offset_set = set(offsets)
    for instruction in decoded.instructions:
        if instruction.jump_target is not None and instruction.jump_target not in offset_set:
            raise ValueError("jump target is not an instruction")
    previous_start = -1
    for handler in decoded.exception_handlers:
        if (
            handler.start_offset < previous_start
            or handler.start_offset not in offset_set
            or (handler.end_offset not in offset_set and handler.end_offset != decoded.code_size)
            or handler.end_offset <= handler.start_offset
            or handler.target_offset not in offset_set
            or not 0 <= handler.stack_depth <= decoded.stack_size
            or type(handler.preserve_lasti) is not bool
        ):
            raise ValueError("invalid exception handler")
        previous_start = handler.start_offset
    verify_source_map(decoded.source_map, expected_offsets=offsets)
