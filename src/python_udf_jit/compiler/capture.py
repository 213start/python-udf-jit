from __future__ import annotations

import dis
import hashlib
import inspect
import json
import platform
import types
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


FLOAT64 = "float64"
LOCKED_TARGET_PYTHON = "3.14.3"


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


def _code_identity(function: types.FunctionType) -> str:
    constants = []
    for value in function.__code__.co_consts:
        if value is None:
            constants.append(["none", None])
        elif type(value) is float:
            constants.append(["float", value.hex()])
        else:
            constants.append([type(value).__name__, repr(value)])
    document = json.dumps(constants, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
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
    forbidden_flags = inspect.CO_VARARGS | inspect.CO_VARKEYWORDS | inspect.CO_GENERATOR | inspect.CO_COROUTINE
    if (
        code.co_argcount != 1
        or code.co_kwonlyargcount != 0
        or code.co_flags & forbidden_flags
    ):
        _reject(CaptureRejectCode.UNSUPPORTED_SIGNATURE)
    if code.co_freevars or code.co_cellvars:
        _reject(CaptureRejectCode.CLOSURE_DEPENDENCY)

    instructions = tuple(dis.get_instructions(code, show_caches=False))
    if any(instruction.opcode in dis.hasjabs or instruction.opcode in dis.hasjrel for instruction in instructions):
        _reject(CaptureRejectCode.CONTROL_FLOW)
    if any(instruction.opname in {"CALL", "CALL_KW", "CALL_FUNCTION_EX"} for instruction in instructions):
        _reject(CaptureRejectCode.OPAQUE_CALL)
    if any(instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"} for instruction in instructions):
        _reject(CaptureRejectCode.GLOBAL_DEPENDENCY)
    if any(instruction.opname in {"COMPARE_OP", "IS_OP", "CONTAINS_OP"} for instruction in instructions):
        _reject(CaptureRejectCode.UNSUPPORTED_OPERATOR)

    captured: list[CaptureInstruction] = []
    stack_depth = 0
    returned = False
    for instruction in instructions:
        opname = instruction.opname
        if opname == "RESUME":
            continue
        if opname in {"LOAD_FAST", "LOAD_FAST_BORROW"}:
            if instruction.arg != 0:
                _reject(CaptureRejectCode.UNSUPPORTED_SIGNATURE)
            captured.append(CaptureInstruction("arg.load"))
            stack_depth += 1
        elif opname == "LOAD_CONST":
            if type(instruction.argval) is not float:
                _reject(CaptureRejectCode.INVALID_CONSTANT, type(instruction.argval).__name__)
            captured.append(CaptureInstruction("const.f64", instruction.argval))
            stack_depth += 1
        elif opname == "BINARY_OP":
            operation = {"+": "add.f64", "-": "sub.f64", "*": "mul.f64"}.get(instruction.argrepr)
            if operation is None:
                _reject(CaptureRejectCode.UNSUPPORTED_OPERATOR, instruction.argrepr)
            if stack_depth < 2:
                _reject(CaptureRejectCode.INVALID_STACK)
            captured.append(CaptureInstruction(operation))
            stack_depth -= 1
        elif opname == "RETURN_VALUE":
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
    )


def try_capture(request: CaptureRequest) -> CaptureResult:
    try:
        return CaptureResult(True, capture(request))
    except CaptureRejected as error:
        return CaptureResult(False, reject_code=error.code, reject_detail=error.detail)
