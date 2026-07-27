from __future__ import annotations

import builtins
import hashlib
import inspect
import types
from dataclasses import dataclass
from enum import StrEnum

from python_udf_jit.compiler.bytecode_decoder import DecodedBytecode


class Effect(StrEnum):
    PURE = "pure"
    MAY_RAISE = "may_raise"
    SIDE_EFFECT = "side_effect"


class CallKind(StrEnum):
    PURE_BUILTIN = "pure_builtin"
    CONTROLLED_STRING = "controlled_string"
    SMALL_FUNCTION = "small_function"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class CallModel:
    bytecode_offset: int
    kind: CallKind
    effect: Effect
    name_sha256: str
    modeled: bool
    may_raise: bool


_PURE_BUILTINS = {
    "abs": builtins.abs,
    "bool": builtins.bool,
    "float": builtins.float,
    "int": builtins.int,
    "len": builtins.len,
    "max": builtins.max,
    "min": builtins.min,
    "str": builtins.str,
}
_CONTROLLED_STRING_METHODS = {
    "endswith",
    "lower",
    "startswith",
    "strip",
    "upper",
}


def _hash_name(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _name_at(
    decoded: DecodedBytecode,
    instruction_index: int,
) -> tuple[str, int] | None:
    instructions = decoded.instructions
    lower_bound = max(0, instruction_index - 8)
    for index in range(instruction_index - 1, lower_bound - 1, -1):
        instruction = instructions[index]
        if instruction.operation in {
            "branch.always",
            "return.constant",
            "return.value",
        } or instruction.operation.startswith("branch.if_"):
            return None
        if instruction.opcode_name == "LOAD_GLOBAL":
            assert instruction.argument is not None
            return "global", instruction.argument >> 1
        if instruction.opcode_name == "LOAD_ATTR":
            assert instruction.argument is not None
            return "method", instruction.argument >> 1
    return None


def _global_model(
    *,
    offset: int,
    name: str,
    function: types.FunctionType,
) -> CallModel:
    namespace = function.__globals__
    if name in namespace:
        value = namespace[name]
        if value is function:
            return CallModel(
                offset,
                CallKind.OPAQUE,
                Effect.SIDE_EFFECT,
                _hash_name(name),
                False,
                True,
            )
        if type(value) is types.FunctionType:
            code = value.__code__
            modeled = (
                len(code.co_code) <= 256
                and not code.co_freevars
                and not code.co_cellvars
                and not (
                    code.co_flags
                    & (
                        inspect.CO_GENERATOR
                        | inspect.CO_COROUTINE
                        | inspect.CO_ASYNC_GENERATOR
                    )
                )
            )
            return CallModel(
                offset,
                CallKind.SMALL_FUNCTION if modeled else CallKind.OPAQUE,
                Effect.MAY_RAISE if modeled else Effect.SIDE_EFFECT,
                _hash_name(name),
                modeled,
                True,
            )
        expected = _PURE_BUILTINS.get(name)
        if expected is not None and value is expected:
            return CallModel(
                offset,
                CallKind.PURE_BUILTIN,
                Effect.MAY_RAISE,
                _hash_name(name),
                True,
                True,
            )
    elif name in _PURE_BUILTINS:
        return CallModel(
            offset,
            CallKind.PURE_BUILTIN,
            Effect.MAY_RAISE,
            _hash_name(name),
            True,
            True,
        )
    return CallModel(
        offset,
        CallKind.OPAQUE,
        Effect.SIDE_EFFECT,
        _hash_name(name),
        False,
        True,
    )


def classify_calls(
    decoded: DecodedBytecode,
    function: types.FunctionType,
) -> dict[int, CallModel]:
    """Classify calls from static code metadata without invoking a callable."""

    if type(function) is not types.FunctionType:
        raise TypeError("call model requires an exact function")
    names = function.__code__.co_names
    models: dict[int, CallModel] = {}
    for index, instruction in enumerate(decoded.instructions):
        if instruction.operation != "call.opaque":
            continue
        source = _name_at(decoded, index)
        if source is None:
            models[instruction.offset] = CallModel(
                instruction.offset,
                CallKind.OPAQUE,
                Effect.SIDE_EFFECT,
                _hash_name("unknown"),
                False,
                True,
            )
            continue
        source_kind, name_index = source
        if not 0 <= name_index < len(names):
            raise ValueError("call name index is outside co_names")
        name = names[name_index]
        if type(name) is not str:
            raise ValueError("call name is not an exact string")
        if source_kind == "method":
            modeled = name in _CONTROLLED_STRING_METHODS
            models[instruction.offset] = CallModel(
                instruction.offset,
                (
                    CallKind.CONTROLLED_STRING
                    if modeled
                    else CallKind.OPAQUE
                ),
                Effect.MAY_RAISE if modeled else Effect.SIDE_EFFECT,
                _hash_name(name),
                modeled,
                True,
            )
        else:
            models[instruction.offset] = _global_model(
                offset=instruction.offset,
                name=name,
                function=function,
            )
    return models
