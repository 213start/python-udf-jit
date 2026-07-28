from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
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
    if not captured.instructions:
        raise ValueError("capture has no legacy F64 lowering")
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


SEMANTIC_CORE_IR_VERSION = 2
MAX_SEMANTIC_NODES = 4096
MAX_SEMANTIC_BLOCKS = 1024
MAX_SEMANTIC_EDGES = 16_384
MAX_SEMANTIC_VALUES = 16_384
MAX_SEMANTIC_TEXT_BYTES = 4096


class LogicalType(StrEnum):
    BOOL = "bool"
    INT64 = "int64"
    FLOAT64 = "float64"
    STRING = "string"
    BYTES = "bytes"
    TUPLE = "tuple"
    LIST = "list"
    OBJECT = "object"
    UNKNOWN = "unknown"


class Nullability(StrEnum):
    KNOWN_NULL = "known_null"
    NON_NULL = "non_null"
    NULLABLE = "nullable"


class EffectKind(StrEnum):
    PURE = "pure"
    READ_GLOBAL = "read_global"
    WRITE_GLOBAL = "write_global"
    IO = "io"
    NONDETERMINISTIC = "nondeterministic"
    PYTHON = "python"


class Determinism(StrEnum):
    DETERMINISTIC = "deterministic"
    NONDETERMINISTIC = "nondeterministic"
    UNKNOWN = "unknown"


class LiteralKind(StrEnum):
    NONE = "none"
    BOOL = "bool"
    INT64 = "int64"
    FLOAT64 = "float64"
    STRING = "string"
    BYTES = "bytes"


@dataclass(frozen=True)
class SemanticLiteral:
    kind: LiteralKind
    encoded_value: str

    @classmethod
    def from_value(
        cls,
        value: None | bool | int | float | str | bytes,
    ) -> "SemanticLiteral":
        if value is None:
            return cls(LiteralKind.NONE, "")
        if type(value) is bool:
            return cls(LiteralKind.BOOL, "true" if value else "false")
        if type(value) is int:
            if not -(1 << 63) <= value < (1 << 63):
                raise ValueError("int64 literal out of range")
            return cls(LiteralKind.INT64, str(value))
        if type(value) is float:
            return cls(LiteralKind.FLOAT64, value.hex())
        if type(value) is str:
            if len(value.encode("utf-8")) > MAX_SEMANTIC_TEXT_BYTES:
                raise ValueError("string literal size limit")
            return cls(LiteralKind.STRING, value)
        if type(value) is bytes:
            if len(value) > MAX_SEMANTIC_TEXT_BYTES:
                raise ValueError("bytes literal size limit")
            return cls(LiteralKind.BYTES, value.hex())
        raise TypeError("unsupported semantic literal")

    @property
    def value(self) -> None | bool | int | float | str | bytes:
        if self.kind is LiteralKind.NONE:
            return None
        if self.kind is LiteralKind.BOOL:
            if self.encoded_value not in {"true", "false"}:
                raise ValueError("invalid bool literal")
            return self.encoded_value == "true"
        if self.kind is LiteralKind.INT64:
            value = int(self.encoded_value)
            if not -(1 << 63) <= value < (1 << 63):
                raise ValueError("int64 literal out of range")
            return value
        if self.kind is LiteralKind.FLOAT64:
            return float.fromhex(self.encoded_value)
        if self.kind is LiteralKind.STRING:
            return self.encoded_value
        if self.kind is LiteralKind.BYTES:
            try:
                return bytes.fromhex(self.encoded_value)
            except ValueError as error:
                raise ValueError("invalid bytes literal") from error
        raise ValueError("unknown semantic literal kind")

    def to_document(self) -> dict[str, str]:
        return {
            "encoded_value": self.encoded_value,
            "kind": self.kind.value,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticLiteral":
        if (
            not isinstance(document, dict)
            or set(document) != {"encoded_value", "kind"}
            or not isinstance(document["encoded_value"], str)
            or not isinstance(document["kind"], str)
        ):
            raise ValueError("invalid semantic literal fields")
        literal = cls(
            LiteralKind(document["kind"]),
            document["encoded_value"],
        )
        encoded_size = len(literal.encoded_value.encode("utf-8"))
        limit = (
            MAX_SEMANTIC_TEXT_BYTES * 2
            if literal.kind is LiteralKind.BYTES
            else MAX_SEMANTIC_TEXT_BYTES
        )
        if encoded_size > limit:
            raise ValueError("semantic literal size limit")
        literal.value
        return literal


@dataclass(frozen=True)
class SemanticOperation:
    operation_id: str
    block_id: str
    op: str
    operands: tuple[str, ...]
    result_id: str | None
    result_type: LogicalType
    nullability: Nullability
    effect: EffectKind
    may_raise: bool
    exception_order: int | None
    determinism: Determinism
    attributes: tuple[tuple[str, str], ...] = ()
    literal: SemanticLiteral | None = None
    source_offset: int | None = None
    python_region_id: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "attributes": [list(item) for item in self.attributes],
            "block_id": self.block_id,
            "determinism": self.determinism.value,
            "effect": self.effect.value,
            "exception_order": self.exception_order,
            "literal": None if self.literal is None else self.literal.to_document(),
            "may_raise": self.may_raise,
            "nullability": self.nullability.value,
            "op": self.op,
            "operands": list(self.operands),
            "operation_id": self.operation_id,
            "python_region_id": self.python_region_id,
            "result_id": self.result_id,
            "result_type": self.result_type.value,
            "source_offset": self.source_offset,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticOperation":
        expected = {
            "attributes",
            "block_id",
            "determinism",
            "effect",
            "exception_order",
            "literal",
            "may_raise",
            "nullability",
            "op",
            "operands",
            "operation_id",
            "python_region_id",
            "result_id",
            "result_type",
            "source_offset",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic operation fields")
        strings = ("block_id", "op", "operation_id")
        if any(not isinstance(document[name], str) for name in strings):
            raise ValueError("invalid semantic operation string")
        optional_strings = ("python_region_id", "result_id")
        if any(
            document[name] is not None
            and not isinstance(document[name], str)
            for name in optional_strings
        ):
            raise ValueError("invalid semantic operation optional string")
        optional_integers = ("exception_order", "source_offset")
        if any(
            document[name] is not None
            and type(document[name]) is not int
            for name in optional_integers
        ):
            raise ValueError("invalid semantic operation optional integer")
        operands = document["operands"]
        attributes = document["attributes"]
        if (
            not isinstance(operands, list)
            or any(not isinstance(value, str) for value in operands)
            or len(operands) > MAX_SEMANTIC_VALUES
            or not isinstance(attributes, list)
            or len(attributes) > 16
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) for value in item)
                for item in attributes
            )
            or type(document["may_raise"]) is not bool
        ):
            raise ValueError("invalid semantic operation sequence")
        return cls(
            document["operation_id"],
            document["block_id"],
            document["op"],
            tuple(operands),
            document["result_id"],
            LogicalType(document["result_type"]),
            Nullability(document["nullability"]),
            EffectKind(document["effect"]),
            document["may_raise"],
            document["exception_order"],
            Determinism(document["determinism"]),
            tuple((item[0], item[1]) for item in attributes),
            (
                None
                if document["literal"] is None
                else SemanticLiteral.from_document(document["literal"])
            ),
            document["source_offset"],
            document["python_region_id"],
        )

    def attribute(self, name: str) -> str | None:
        return dict(self.attributes).get(name)


@dataclass(frozen=True)
class SemanticBlock:
    block_id: str
    operation_ids: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "operation_ids": list(self.operation_ids),
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticBlock":
        if (
            not isinstance(document, dict)
            or set(document) != {"block_id", "operation_ids"}
            or not isinstance(document["block_id"], str)
            or not isinstance(document["operation_ids"], list)
            or len(document["operation_ids"]) > MAX_SEMANTIC_NODES
            or any(
                not isinstance(value, str)
                for value in document["operation_ids"]
            )
        ):
            raise ValueError("invalid semantic block")
        return cls(
            document["block_id"],
            tuple(document["operation_ids"]),
        )


@dataclass(frozen=True)
class SemanticControlEdge:
    source_block: str
    target_block: str
    kind: str

    def to_document(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source_block": self.source_block,
            "target_block": self.target_block,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticControlEdge":
        expected = {"kind", "source_block", "target_block"}
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or any(
                not isinstance(document[name], str)
                for name in expected
            )
        ):
            raise ValueError("invalid semantic control edge")
        return cls(
            document["source_block"],
            document["target_block"],
            document["kind"],
        )


@dataclass(frozen=True)
class SemanticPythonRegion:
    region_id: str
    operation_id: str
    live_in: tuple[str, ...]
    live_out: tuple[str, ...]
    resume_id: str
    effect: EffectKind
    may_raise: bool
    handler_blocks: tuple[str, ...] = ()
    source_start: int | None = None
    source_end: int | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "handler_blocks": list(self.handler_blocks),
            "live_in": list(self.live_in),
            "live_out": list(self.live_out),
            "may_raise": self.may_raise,
            "operation_id": self.operation_id,
            "region_id": self.region_id,
            "resume_id": self.resume_id,
            "source_end": self.source_end,
            "source_start": self.source_start,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticPythonRegion":
        expected = {
            "effect",
            "handler_blocks",
            "live_in",
            "live_out",
            "may_raise",
            "operation_id",
            "region_id",
            "resume_id",
            "source_end",
            "source_start",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic Python region fields")
        for name in ("operation_id", "region_id", "resume_id"):
            if not isinstance(document[name], str):
                raise ValueError("invalid semantic Python region string")
        for name in ("live_in", "live_out", "handler_blocks"):
            if (
                not isinstance(document[name], list)
                or len(document[name]) > MAX_SEMANTIC_VALUES
                or any(
                    not isinstance(value, str)
                    for value in document[name]
                )
            ):
                raise ValueError("invalid semantic Python region sequence")
        for name in ("source_start", "source_end"):
            if (
                document[name] is not None
                and type(document[name]) is not int
            ):
                raise ValueError("invalid semantic Python region source")
        if type(document["may_raise"]) is not bool:
            raise ValueError("invalid semantic Python region flag")
        return cls(
            document["region_id"],
            document["operation_id"],
            tuple(document["live_in"]),
            tuple(document["live_out"]),
            document["resume_id"],
            EffectKind(document["effect"]),
            document["may_raise"],
            tuple(document["handler_blocks"]),
            document["source_start"],
            document["source_end"],
        )


@dataclass(frozen=True)
class SemanticCoreModule:
    format_version: int
    function_id: str
    entry_block: str
    input_types: tuple[LogicalType, ...]
    input_nullability: tuple[Nullability, ...]
    output_type: LogicalType
    output_nullability: Nullability
    blocks: tuple[SemanticBlock, ...]
    control_edges: tuple[SemanticControlEdge, ...]
    operations: tuple[SemanticOperation, ...]
    python_regions: tuple[SemanticPythonRegion, ...]
    return_operation_id: str
    semantic_hash: str

    def semantic_document(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_document() for block in self.blocks],
            "control_edges": [
                edge.to_document() for edge in self.control_edges
            ],
            "entry_block": self.entry_block,
            "format_version": self.format_version,
            "function_id": self.function_id,
            "input_nullability": [
                value.value for value in self.input_nullability
            ],
            "input_types": [value.value for value in self.input_types],
            "operations": [
                operation.to_document() for operation in self.operations
            ],
            "output_nullability": self.output_nullability.value,
            "output_type": self.output_type.value,
            "python_regions": [
                region.to_document() for region in self.python_regions
            ],
            "return_operation_id": self.return_operation_id,
        }

    def recompute_semantic_hash(self) -> str:
        return hashlib.sha256(
            b"python-udf-jit-semantic-core-v2\0"
            + _canonical_bytes(self.semantic_document())
        ).hexdigest()

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_document(cls, document: object) -> "SemanticCoreModule":
        expected = {
            "blocks",
            "control_edges",
            "entry_block",
            "format_version",
            "function_id",
            "input_nullability",
            "input_types",
            "operations",
            "output_nullability",
            "output_type",
            "python_regions",
            "return_operation_id",
            "semantic_hash",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid semantic Core IR module fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid semantic Core IR version")
        for name in (
            "entry_block",
            "function_id",
            "return_operation_id",
            "semantic_hash",
        ):
            if not isinstance(document[name], str):
                raise ValueError("invalid semantic Core IR string")
        for name in (
            "blocks",
            "control_edges",
            "input_nullability",
            "input_types",
            "operations",
            "python_regions",
        ):
            if not isinstance(document[name], list):
                raise ValueError("invalid semantic Core IR sequence")
        if (
            len(document["blocks"]) > MAX_SEMANTIC_BLOCKS
            or len(document["control_edges"]) > MAX_SEMANTIC_EDGES
            or len(document["input_types"]) > 256
            or len(document["operations"]) > MAX_SEMANTIC_NODES
            or len(document["python_regions"]) > MAX_SEMANTIC_NODES
        ):
            raise ValueError("semantic Core IR size limit")
        result = cls(
            document["format_version"],
            document["function_id"],
            document["entry_block"],
            tuple(LogicalType(value) for value in document["input_types"]),
            tuple(
                Nullability(value)
                for value in document["input_nullability"]
            ),
            LogicalType(document["output_type"]),
            Nullability(document["output_nullability"]),
            tuple(
                SemanticBlock.from_document(value)
                for value in document["blocks"]
            ),
            tuple(
                SemanticControlEdge.from_document(value)
                for value in document["control_edges"]
            ),
            tuple(
                SemanticOperation.from_document(value)
                for value in document["operations"]
            ),
            tuple(
                SemanticPythonRegion.from_document(value)
                for value in document["python_regions"]
            ),
            document["return_operation_id"],
            document["semantic_hash"],
        )
        from python_udf_jit.compiler.verifier import (
            verify_semantic_module,
        )

        verify_semantic_module(result)
        return result


def build_semantic_module(
    *,
    function_id: str,
    entry_block: str,
    input_types: tuple[LogicalType, ...],
    input_nullability: tuple[Nullability, ...],
    output_type: LogicalType,
    output_nullability: Nullability,
    blocks: tuple[SemanticBlock, ...],
    control_edges: tuple[SemanticControlEdge, ...],
    operations: tuple[SemanticOperation, ...],
    python_regions: tuple[SemanticPythonRegion, ...] = (),
    return_operation_id: str,
) -> SemanticCoreModule:
    provisional = SemanticCoreModule(
        SEMANTIC_CORE_IR_VERSION,
        function_id,
        entry_block,
        input_types,
        input_nullability,
        output_type,
        output_nullability,
        blocks,
        control_edges,
        operations,
        python_regions,
        return_operation_id,
        "",
    )
    result = SemanticCoreModule(
        **{
            **provisional.__dict__,
            "semantic_hash": provisional.recompute_semantic_hash(),
        }
    )
    from python_udf_jit.compiler.verifier import verify_semantic_module

    verify_semantic_module(result)
    return result


def rehash_semantic_module(
    module: SemanticCoreModule,
) -> SemanticCoreModule:
    provisional = SemanticCoreModule(
        **{**module.__dict__, "semantic_hash": ""}
    )
    return SemanticCoreModule(
        **{
            **provisional.__dict__,
            "semantic_hash": provisional.recompute_semantic_hash(),
        }
    )
