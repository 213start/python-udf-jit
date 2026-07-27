from __future__ import annotations

import hashlib
import inspect
import json
import platform
import types
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from python_udf_jit.compiler.bytecode_decoder import (
    BytecodeDecodeError,
    DecodeRejectCode,
)
from python_udf_jit.compiler.capture_ir import (
    CaptureFrontend,
    build_capture_frontend,
)
from python_udf_jit.compiler.cfg import CfgBuildError, CfgRejectCode


FLOAT64 = "float64"
LOCKED_TARGET_PYTHON = "3.14.3"
MAX_IDENTITY_DOCUMENT_BYTES = 1 << 20
MAX_IDENTITY_SEQUENCE_ITEMS = 65_536


class CaptureRejectCode(StrEnum):
    SCHEMA_MISMATCH = "schema_mismatch"
    UNSUPPORTED_CALLABLE = "unsupported_callable"
    UNSUPPORTED_SIGNATURE = "unsupported_signature"
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"
    CLOSURE_DEPENDENCY = "closure_dependency"
    GLOBAL_DEPENDENCY = "global_dependency"
    CONTROL_FLOW = "control_flow"
    OPAQUE_CALL = "opaque_call"
    UNSUPPORTED_OPERATOR = "unsupported_operator"
    UNSUPPORTED_OPCODE = "unsupported_opcode"
    INVALID_CONSTANT = "invalid_constant"
    INVALID_STACK = "invalid_stack"
    UNSUPPORTED_BYTECODE_FORMAT = "unsupported_bytecode_format"
    INVALID_BYTECODE = "invalid_bytecode"
    INVALID_EXCEPTION_TABLE = "invalid_exception_table"
    INVALID_LOCATION_TABLE = "invalid_location_table"


class CaptureRejected(ValueError):
    def __init__(self, code: CaptureRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


@dataclass(frozen=True)
class FallbackIdentity:
    module: str
    qualname: str
    code_sha256: str

    def to_document(self) -> dict[str, str]:
        return {
            "code_sha256": self.code_sha256,
            "module": self.module,
            "qualname": self.qualname,
        }

    @classmethod
    def from_document(cls, document: object) -> "FallbackIdentity":
        if not isinstance(document, dict) or set(document) != {
            "code_sha256",
            "module",
            "qualname",
        }:
            raise ValueError("invalid fallback identity fields")
        values = (document["module"], document["qualname"], document["code_sha256"])
        if not all(isinstance(value, str) for value in values):
            raise ValueError("fallback identity fields must be strings")
        identity = cls(*values)
        if (
            not identity.module
            or not identity.qualname
            or len(identity.module.encode("utf-8")) > 4096
            or len(identity.qualname.encode("utf-8")) > 4096
            or len(identity.code_sha256) != 64
            or any(character not in "0123456789abcdef" for character in identity.code_sha256)
        ):
            raise ValueError("invalid fallback identity value")
        return identity


@dataclass(frozen=True)
class CaptureRequest:
    callable_object: Any
    input_types: tuple[str, ...] = (FLOAT64,)
    output_type: str = FLOAT64
    target_python: str = LOCKED_TARGET_PYTHON


@dataclass(frozen=True)
class CaptureInstruction:
    op: str
    literal: float | None = None


@dataclass(frozen=True)
class CaptureIR:
    format_version: int
    parameter_name: str
    input_type: str
    output_type: str
    target_python: str
    capture_runtime_python: str
    instructions: tuple[CaptureInstruction, ...]
    fallback_identity: FallbackIdentity
    frontend: CaptureFrontend


@dataclass(frozen=True)
class CaptureResult:
    supported: bool
    capture_ir: CaptureIR | None = None
    reject_code: CaptureRejectCode | None = None
    reject_detail: str = ""


def _reject(code: CaptureRejectCode, detail: str = "") -> None:
    raise CaptureRejected(code, detail)


def _resolve_user_function(callable_object: Any) -> types.FunctionType:
    if type(callable_object) is types.FunctionType:
        return callable_object
    try:
        namespace = object.__getattribute__(callable_object, "__dict__")
    except (TypeError, AttributeError):
        _reject(CaptureRejectCode.UNSUPPORTED_CALLABLE)
    if type(namespace) is not dict:
        _reject(CaptureRejectCode.UNSUPPORTED_CALLABLE, "nonstandard_namespace")
    method = namespace.get("_method")
    if type(method) is not types.FunctionType:
        _reject(CaptureRejectCode.UNSUPPORTED_CALLABLE, "missing_static_method")
    wrapped = vars(method).get("__wrapped__")
    if type(wrapped) is not types.FunctionType:
        _reject(CaptureRejectCode.UNSUPPORTED_CALLABLE, "missing_single_wrapped_function")
    if vars(wrapped).get("__wrapped__") is not None:
        _reject(CaptureRejectCode.UNSUPPORTED_CALLABLE, "multiple_wrapping_layers")
    return wrapped


def _safe_constant_document(value: object, *, depth: int = 0) -> object:
    if depth > 16:
        _reject(CaptureRejectCode.INVALID_CONSTANT, "nested_constant")
    value_type = type(value)
    if value is None:
        return ["none", None]
    if value is Ellipsis:
        return ["ellipsis", None]
    if value_type is bool:
        return ["bool", value]
    if value_type is int:
        if value.bit_length() > MAX_IDENTITY_DOCUMENT_BYTES * 8:
            _reject(CaptureRejectCode.INVALID_CONSTANT, "integer_budget")
        return ["int", str(value)]
    if value_type is float:
        return ["float", value.hex()]
    if value_type is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if value_type is str:
        if len(value) > MAX_IDENTITY_DOCUMENT_BYTES:
            _reject(CaptureRejectCode.INVALID_CONSTANT, "string_budget")
        return ["str", value]
    if value_type is bytes:
        if len(value) > MAX_IDENTITY_DOCUMENT_BYTES:
            _reject(CaptureRejectCode.INVALID_CONSTANT, "bytes_budget")
        return ["bytes", value.hex()]
    if value_type is tuple:
        if len(value) > MAX_IDENTITY_SEQUENCE_ITEMS:
            _reject(CaptureRejectCode.INVALID_CONSTANT, "tuple_budget")
        return [
            "tuple",
            [_safe_constant_document(item, depth=depth + 1) for item in value],
        ]
    if value_type is frozenset:
        if len(value) > MAX_IDENTITY_SEQUENCE_ITEMS:
            _reject(CaptureRejectCode.INVALID_CONSTANT, "frozenset_budget")
        encoded = [
            _safe_constant_document(item, depth=depth + 1) for item in value
        ]
        return [
            "frozenset",
            sorted(
                encoded,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ),
            ),
        ]
    _reject(CaptureRejectCode.INVALID_CONSTANT, "unsupported_exact_type")


def _code_identity(function: types.FunctionType) -> str:
    constants = [
        _safe_constant_document(value) for value in function.__code__.co_consts
    ]
    document = json.dumps(constants, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(document) > MAX_IDENTITY_DOCUMENT_BYTES:
        _reject(CaptureRejectCode.INVALID_CONSTANT, "identity_document_budget")
    digest = hashlib.sha256()
    digest.update(function.__code__.co_code)
    digest.update(document.encode("ascii"))
    return digest.hexdigest()


def _validate_request_before_callable(request: CaptureRequest) -> None:
    if request.input_types != (FLOAT64,) or request.output_type != FLOAT64:
        _reject(CaptureRejectCode.SCHEMA_MISMATCH)
    if request.target_python != LOCKED_TARGET_PYTHON:
        _reject(CaptureRejectCode.PYTHON_VERSION_MISMATCH, request.target_python)
    runtime = platform.python_version_tuple()
    if runtime[:2] != ("3", "14"):
        _reject(CaptureRejectCode.PYTHON_VERSION_MISMATCH, ".".join(runtime))


def capture(request: CaptureRequest) -> CaptureIR:
    """Statically decode one float64 straight-line function without invoking it."""

    _validate_request_before_callable(request)
    function = _resolve_user_function(request.callable_object)
    code = function.__code__
    forbidden_flags = (
        inspect.CO_VARARGS
        | inspect.CO_VARKEYWORDS
        | inspect.CO_GENERATOR
        | inspect.CO_COROUTINE
    )
    if (
        code.co_argcount != 1
        or code.co_kwonlyargcount != 0
        or code.co_flags & forbidden_flags
    ):
        _reject(CaptureRejectCode.UNSUPPORTED_SIGNATURE)
    if code.co_freevars or code.co_cellvars:
        _reject(CaptureRejectCode.CLOSURE_DEPENDENCY)

    try:
        frontend = build_capture_frontend(code)
    except BytecodeDecodeError as error:
        code_map = {
            DecodeRejectCode.UNSUPPORTED_FORMAT: CaptureRejectCode.UNSUPPORTED_BYTECODE_FORMAT,
            DecodeRejectCode.UNKNOWN_OPCODE: CaptureRejectCode.UNSUPPORTED_OPCODE,
            DecodeRejectCode.INVALID_EXCEPTION_TABLE: CaptureRejectCode.INVALID_EXCEPTION_TABLE,
            DecodeRejectCode.INVALID_LOCATION_TABLE: CaptureRejectCode.INVALID_LOCATION_TABLE,
        }
        _reject(code_map.get(error.code, CaptureRejectCode.INVALID_BYTECODE), error.detail)
    except CfgBuildError as error:
        code_map = {
            CfgRejectCode.STACK_UNDERFLOW: CaptureRejectCode.INVALID_STACK,
            CfgRejectCode.STACK_OVERFLOW: CaptureRejectCode.INVALID_STACK,
            CfgRejectCode.STACK_IMBALANCE: CaptureRejectCode.INVALID_STACK,
        }
        _reject(code_map.get(error.code, CaptureRejectCode.CONTROL_FLOW), error.detail)

    instructions = frontend.decoded_bytecode.instructions
    if any(instruction.jump_target is not None for instruction in instructions):
        _reject(CaptureRejectCode.CONTROL_FLOW)
    if any(instruction.operation == "call.opaque" for instruction in instructions):
        _reject(CaptureRejectCode.OPAQUE_CALL)
    if any(instruction.operation == "global.load" for instruction in instructions):
        _reject(CaptureRejectCode.GLOBAL_DEPENDENCY)
    if any(instruction.operation.startswith("compare.") for instruction in instructions):
        _reject(CaptureRejectCode.UNSUPPORTED_OPERATOR)

    captured: list[CaptureInstruction] = []
    stack_depth = 0
    returned = False
    for instruction in instructions:
        opname = instruction.opcode_name
        if opname == "RESUME":
            continue
        if instruction.operation == "local.load":
            if instruction.argument != 0:
                _reject(CaptureRejectCode.UNSUPPORTED_SIGNATURE)
            captured.append(CaptureInstruction("arg.load"))
            stack_depth += 1
        elif instruction.operation == "constant.load":
            assert instruction.argument is not None
            value = code.co_consts[instruction.argument]
            if type(value) is not float:
                _reject(
                    CaptureRejectCode.INVALID_CONSTANT,
                    instruction.constant_kind or "unsupported_constant_type",
                )
            captured.append(CaptureInstruction("const.f64", value))
            stack_depth += 1
        elif instruction.operation.startswith("binary."):
            operation = {
                "binary.add": "add.f64",
                "binary.subtract": "sub.f64",
                "binary.multiply": "mul.f64",
            }.get(instruction.operation)
            if operation is None:
                _reject(CaptureRejectCode.UNSUPPORTED_OPERATOR, instruction.operation)
            if stack_depth < 2:
                _reject(CaptureRejectCode.INVALID_STACK)
            captured.append(CaptureInstruction(operation))
            stack_depth -= 1
        elif instruction.operation == "return.value":
            if returned or stack_depth != 1:
                _reject(CaptureRejectCode.INVALID_STACK)
            captured.append(CaptureInstruction("return"))
            stack_depth = 0
            returned = True
        else:
            _reject(CaptureRejectCode.UNSUPPORTED_OPCODE, opname)
    if not returned or stack_depth != 0:
        _reject(CaptureRejectCode.INVALID_STACK)

    return CaptureIR(
        format_version=1,
        parameter_name=code.co_varnames[0],
        input_type=FLOAT64,
        output_type=FLOAT64,
        target_python=request.target_python,
        capture_runtime_python=platform.python_version(),
        instructions=tuple(captured),
        fallback_identity=FallbackIdentity(
            function.__module__, function.__qualname__, _code_identity(function)
        ),
        frontend=frontend,
    )


def capture_frontend(callable_object: Any) -> CaptureFrontend:
    """Build the versioned static frontend without applying legacy lowering gates."""

    function = _resolve_user_function(callable_object)
    return build_capture_frontend(function.__code__)


def try_capture(request: CaptureRequest) -> CaptureResult:
    try:
        return CaptureResult(True, capture(request))
    except CaptureRejected as error:
        return CaptureResult(False, reject_code=error.code, reject_detail=error.detail)
