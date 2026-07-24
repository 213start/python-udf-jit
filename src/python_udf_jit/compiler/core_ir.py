from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.capture import CaptureIR, FallbackIdentity


CORE_IR_VERSION = 1


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class CoreNode:
    result_id: str | None
    op: str
    operands: tuple[str, ...]
    literal: float | None
    result_type: str

    def to_document(self) -> dict[str, Any]:
        return {
            "literal_hex": None if self.literal is None else self.literal.hex(),
            "op": self.op,
            "operands": list(self.operands),
            "result_id": self.result_id,
            "result_type": self.result_type,
        }

    @classmethod
    def from_document(cls, document: object) -> "CoreNode":
        expected = {"literal_hex", "op", "operands", "result_id", "result_type"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid Core IR node fields")
        if not isinstance(document["op"], str) or not isinstance(document["result_type"], str):
            raise ValueError("invalid Core IR node strings")
        result_id = document["result_id"]
        if result_id is not None and not isinstance(result_id, str):
            raise ValueError("invalid Core IR result id")
        operands = document["operands"]
        if not isinstance(operands, list) or not all(isinstance(value, str) for value in operands):
            raise ValueError("invalid Core IR operands")
        literal_hex = document["literal_hex"]
        if literal_hex is None:
            literal = None
        elif isinstance(literal_hex, str):
            try:
                literal = float.fromhex(literal_hex)
            except ValueError as error:
                raise ValueError("invalid float literal") from error
        else:
            raise ValueError("invalid Core IR literal")
        return cls(result_id, document["op"], tuple(operands), literal, document["result_type"])


@dataclass(frozen=True)
class CoreUdfModule:
    format_version: int
    input_type: str
    output_type: str
    effect: str
    nodes: tuple[CoreNode, ...]
    return_value: str
    semantic_hash: str
    fallback_identity: FallbackIdentity

    def semantic_document(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "format_version": self.format_version,
            "input_type": self.input_type,
            "nodes": [node.to_document() for node in self.nodes],
            "output_type": self.output_type,
            "return_value": self.return_value,
        }

    def recompute_semantic_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.semantic_document())).hexdigest()

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "fallback_identity": self.fallback_identity.to_document(),
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_document(cls, document: object) -> "CoreUdfModule":
        expected = {
            "effect",
            "fallback_identity",
            "format_version",
            "input_type",
            "nodes",
            "output_type",
            "return_value",
            "semantic_hash",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid Core IR module fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid Core IR format version")
        strings = (
            document["input_type"],
            document["output_type"],
            document["effect"],
            document["return_value"],
            document["semantic_hash"],
        )
        if not all(isinstance(value, str) for value in strings):
            raise ValueError("invalid Core IR module strings")
        node_documents = document["nodes"]
        if not isinstance(node_documents, list):
            raise ValueError("invalid Core IR nodes")
        return cls(
            document["format_version"],
            document["input_type"],
            document["output_type"],
            document["effect"],
            tuple(CoreNode.from_document(node) for node in node_documents),
            document["return_value"],
            document["semantic_hash"],
            FallbackIdentity.from_document(document["fallback_identity"]),
        )


def lower_capture(captured: CaptureIR) -> CoreUdfModule:
    stack: list[str] = []
    nodes: list[CoreNode] = []
    next_value = 0
    return_value = ""
    for instruction in captured.instructions:
        if instruction.op in {"arg.load", "const.f64"}:
            result_id = f"%{next_value}"
            next_value += 1
            nodes.append(CoreNode(result_id, instruction.op, (), instruction.literal, "float64"))
            stack.append(result_id)
        elif instruction.op in {"add.f64", "sub.f64", "mul.f64"}:
            right = stack.pop()
            left = stack.pop()
            result_id = f"%{next_value}"
            next_value += 1
            nodes.append(CoreNode(result_id, instruction.op, (left, right), None, "float64"))
            stack.append(result_id)
        elif instruction.op == "return":
            return_value = stack.pop()
            nodes.append(CoreNode(None, "return", (return_value,), None, "float64"))
        else:
            raise ValueError(f"unrecognized captured operation: {instruction.op}")
    provisional = CoreUdfModule(
        CORE_IR_VERSION,
        captured.input_type,
        captured.output_type,
        "pure",
        tuple(nodes),
        return_value,
        "",
        captured.fallback_identity,
    )
    module = CoreUdfModule(
        **{**provisional.__dict__, "semantic_hash": provisional.recompute_semantic_hash()}
    )
    from python_udf_jit.compiler.verifier import verify_core_module

    verify_core_module(module)
    return module


def reference_execute(module: CoreUdfModule, value: float) -> float:
    from python_udf_jit.compiler.verifier import verify_core_module

    verify_core_module(module)
    if type(value) is not float:
        raise TypeError("reference interpreter accepts one float")
    values: dict[str, float] = {}
    for node in module.nodes:
        if node.op == "arg.load":
            values[node.result_id] = value  # type: ignore[index]
        elif node.op == "const.f64":
            values[node.result_id] = node.literal  # type: ignore[index, assignment]
        elif node.op in {"add.f64", "sub.f64", "mul.f64"}:
            left, right = (values[operand] for operand in node.operands)
            if node.op == "add.f64":
                result = left + right
            elif node.op == "sub.f64":
                result = left - right
            else:
                result = left * right
            values[node.result_id] = result  # type: ignore[index]
    return values[module.return_value]
