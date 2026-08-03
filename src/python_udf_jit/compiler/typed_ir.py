from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    SemanticLiteral,
)


TYPED_SEMANTIC_IR_VERSION = 2
MAX_TYPED_BLOCKS = 1024
MAX_TYPED_EDGES = 16_384
MAX_TYPED_OPERATIONS = 4096
MAX_TYPED_VALUES = 16_384
MAX_RUNTIME_DEPENDENCIES = 256
MAX_TYPE_DEPTH = 8
MAX_TYPE_PARAMETERS = 4
_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class TypeKind(StrEnum):
    SCALAR = "scalar"
    PYTHON_OBJECT = "python_object"
    ITERATOR = "iterator"
    SEQUENCE = "sequence"
    MAPPING = "mapping"
    BUILDER = "builder"


class Exactness(StrEnum):
    EXACT = "exact"
    SUBCLASS = "subclass"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TypeSpec:
    kind: TypeKind
    name: str
    parameters: tuple["TypeSpec", ...] = ()
    exactness: Exactness = Exactness.UNKNOWN
    representation: str = "python_object"
    nullable: bool = False

    @property
    def requires_guard(self) -> bool:
        return self.exactness is Exactness.EXACT and self.kind in {
            TypeKind.PYTHON_OBJECT,
            TypeKind.ITERATOR,
            TypeKind.SEQUENCE,
            TypeKind.MAPPING,
            TypeKind.BUILDER,
        }

    def verify(self, *, depth: int = 0) -> None:
        if (
            depth > MAX_TYPE_DEPTH
            or _TYPE_NAME.fullmatch(self.name) is None
            or _TYPE_NAME.fullmatch(self.representation) is None
            or len(self.parameters) > MAX_TYPE_PARAMETERS
            or type(self.nullable) is not bool
        ):
            raise ValueError("invalid typed IR type")
        expected_parameters = {
            TypeKind.SCALAR: 0,
            TypeKind.PYTHON_OBJECT: 0,
            TypeKind.ITERATOR: 1,
            TypeKind.SEQUENCE: 1,
            TypeKind.MAPPING: 2,
            TypeKind.BUILDER: 1,
        }[self.kind]
        if len(self.parameters) != expected_parameters:
            raise ValueError("invalid typed IR type arity")
        for parameter in self.parameters:
            parameter.verify(depth=depth + 1)

    def to_document(self) -> dict[str, object]:
        self.verify()
        return {
            "exactness": self.exactness.value,
            "kind": self.kind.value,
            "name": self.name,
            "nullable": self.nullable,
            "parameters": [value.to_document() for value in self.parameters],
            "representation": self.representation,
        }

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        depth: int = 0,
    ) -> "TypeSpec":
        expected = {
            "exactness",
            "kind",
            "name",
            "nullable",
            "parameters",
            "representation",
        }
        if (
            depth > MAX_TYPE_DEPTH
            or not isinstance(document, dict)
            or set(document) != expected
            or not isinstance(document["name"], str)
            or not isinstance(document["representation"], str)
            or type(document["nullable"]) is not bool
            or not isinstance(document["parameters"], list)
            or len(document["parameters"]) > MAX_TYPE_PARAMETERS
        ):
            raise ValueError("invalid typed IR type document")
        result = cls(
            TypeKind(document["kind"]),
            document["name"],
            tuple(
                cls.from_document(value, depth=depth + 1)
                for value in document["parameters"]
            ),
            Exactness(document["exactness"]),
            document["representation"],
            document["nullable"],
        )
        result.verify(depth=depth)
        return result


BOOL = TypeSpec(
    TypeKind.SCALAR,
    "bool",
    exactness=Exactness.EXACT,
    representation="unboxed",
)
INT64 = TypeSpec(
    TypeKind.SCALAR,
    "int64",
    exactness=Exactness.EXACT,
    representation="unboxed",
)
FLOAT64 = TypeSpec(
    TypeKind.SCALAR,
    "float64",
    exactness=Exactness.EXACT,
    representation="unboxed",
)
UNICODE_SCALAR = TypeSpec(
    TypeKind.SCALAR,
    "unicode.scalar",
    exactness=Exactness.EXACT,
    representation="unboxed",
)
EXACT_UNICODE = TypeSpec(
    TypeKind.SEQUENCE,
    "str",
    (UNICODE_SCALAR,),
    Exactness.EXACT,
    "python_object",
)


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _attributes_document(
    attributes: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in attributes]


def _parse_attributes(document: object) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(document, list)
        or len(document) > 16
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or any(not isinstance(value, str) for value in item)
            for item in document
        )
    ):
        raise ValueError("invalid typed operation attributes")
    return tuple((item[0], item[1]) for item in document)


@dataclass(frozen=True)
class TypedBlockArgument:
    value_id: str
    type: TypeSpec

    def to_document(self) -> dict[str, object]:
        return {"type": self.type.to_document(), "value_id": self.value_id}

    @classmethod
    def from_document(cls, document: object) -> "TypedBlockArgument":
        if (
            not isinstance(document, dict)
            or set(document) != {"type", "value_id"}
            or not isinstance(document["value_id"], str)
        ):
            raise ValueError("invalid typed block argument")
        return cls(
            document["value_id"],
            TypeSpec.from_document(document["type"]),
        )


@dataclass(frozen=True)
class TypedBlock:
    block_id: str
    arguments: tuple[TypedBlockArgument, ...]
    operation_ids: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "arguments": [value.to_document() for value in self.arguments],
            "block_id": self.block_id,
            "operation_ids": list(self.operation_ids),
        }

    @classmethod
    def from_document(cls, document: object) -> "TypedBlock":
        if (
            not isinstance(document, dict)
            or set(document) != {"arguments", "block_id", "operation_ids"}
            or not isinstance(document["block_id"], str)
            or not isinstance(document["arguments"], list)
            or len(document["arguments"]) > MAX_TYPED_VALUES
            or not isinstance(document["operation_ids"], list)
            or len(document["operation_ids"]) > MAX_TYPED_OPERATIONS
            or any(
                not isinstance(value, str)
                for value in document["operation_ids"]
            )
        ):
            raise ValueError("invalid typed block")
        return cls(
            document["block_id"],
            tuple(
                TypedBlockArgument.from_document(value)
                for value in document["arguments"]
            ),
            tuple(document["operation_ids"]),
        )


@dataclass(frozen=True)
class TypedControlEdge:
    source_block: str
    target_block: str
    kind: str
    arguments: tuple[str, ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "arguments": list(self.arguments),
            "kind": self.kind,
            "source_block": self.source_block,
            "target_block": self.target_block,
        }

    @classmethod
    def from_document(cls, document: object) -> "TypedControlEdge":
        expected = {"arguments", "kind", "source_block", "target_block"}
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or any(
                not isinstance(document[name], str)
                for name in ("kind", "source_block", "target_block")
            )
            or not isinstance(document["arguments"], list)
            or len(document["arguments"]) > MAX_TYPED_VALUES
            or any(not isinstance(value, str) for value in document["arguments"])
        ):
            raise ValueError("invalid typed control edge")
        return cls(
            document["source_block"],
            document["target_block"],
            document["kind"],
            tuple(document["arguments"]),
        )


@dataclass(frozen=True)
class TypedOperation:
    operation_id: str
    block_id: str
    op: str
    operands: tuple[str, ...]
    result_id: str | None
    result_type: TypeSpec | None
    effect: EffectKind
    may_raise: bool
    exception_order: int | None
    determinism: Determinism
    attributes: tuple[tuple[str, str], ...] = ()
    literal: SemanticLiteral | None = None
    source_offset: int | None = None

    def attribute(self, name: str) -> str | None:
        return dict(self.attributes).get(name)

    def to_document(self) -> dict[str, object]:
        return {
            "attributes": _attributes_document(self.attributes),
            "block_id": self.block_id,
            "determinism": self.determinism.value,
            "effect": self.effect.value,
            "exception_order": self.exception_order,
            "literal": None if self.literal is None else self.literal.to_document(),
            "may_raise": self.may_raise,
            "op": self.op,
            "operands": list(self.operands),
            "operation_id": self.operation_id,
            "result_id": self.result_id,
            "result_type": (
                None if self.result_type is None else self.result_type.to_document()
            ),
            "source_offset": self.source_offset,
        }

    @classmethod
    def from_document(cls, document: object) -> "TypedOperation":
        expected = {
            "attributes",
            "block_id",
            "determinism",
            "effect",
            "exception_order",
            "literal",
            "may_raise",
            "op",
            "operands",
            "operation_id",
            "result_id",
            "result_type",
            "source_offset",
        }
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or any(
                not isinstance(document[name], str)
                for name in ("block_id", "op", "operation_id")
            )
            or document["result_id"] is not None
            and not isinstance(document["result_id"], str)
            or document["exception_order"] is not None
            and type(document["exception_order"]) is not int
            or document["source_offset"] is not None
            and type(document["source_offset"]) is not int
            or type(document["may_raise"]) is not bool
            or not isinstance(document["operands"], list)
            or len(document["operands"]) > MAX_TYPED_VALUES
            or any(not isinstance(value, str) for value in document["operands"])
        ):
            raise ValueError("invalid typed operation")
        return cls(
            document["operation_id"],
            document["block_id"],
            document["op"],
            tuple(document["operands"]),
            document["result_id"],
            (
                None
                if document["result_type"] is None
                else TypeSpec.from_document(document["result_type"])
            ),
            EffectKind(document["effect"]),
            document["may_raise"],
            document["exception_order"],
            Determinism(document["determinism"]),
            _parse_attributes(document["attributes"]),
            (
                None
                if document["literal"] is None
                else SemanticLiteral.from_document(document["literal"])
            ),
            document["source_offset"],
        )


@dataclass(frozen=True)
class TypedSemanticModule:
    format_version: int
    function_id: str
    entry_block: str
    input_types: tuple[TypeSpec, ...]
    output_type: TypeSpec
    blocks: tuple[TypedBlock, ...]
    control_edges: tuple[TypedControlEdge, ...]
    operations: tuple[TypedOperation, ...]
    return_operation_id: str
    runtime_dependency_hashes: tuple[str, ...]
    semantic_hash: str

    def semantic_document(self) -> dict[str, object]:
        return {
            "blocks": [value.to_document() for value in self.blocks],
            "control_edges": [value.to_document() for value in self.control_edges],
            "entry_block": self.entry_block,
            "format_version": self.format_version,
            "function_id": self.function_id,
            "input_types": [value.to_document() for value in self.input_types],
            "operations": [value.to_document() for value in self.operations],
            "output_type": self.output_type.to_document(),
            "return_operation_id": self.return_operation_id,
            "runtime_dependency_hashes": list(self.runtime_dependency_hashes),
        }

    def recompute_semantic_hash(self) -> str:
        return hashlib.sha256(
            b"python-udf-jit-typed-semantic-core-v2\0"
            + _canonical_bytes(self.semantic_document())
        ).hexdigest()

    def to_document(self) -> dict[str, object]:
        return {**self.semantic_document(), "semantic_hash": self.semantic_hash}

    @classmethod
    def from_document(cls, document: object) -> "TypedSemanticModule":
        expected = {
            "blocks",
            "control_edges",
            "entry_block",
            "format_version",
            "function_id",
            "input_types",
            "operations",
            "output_type",
            "return_operation_id",
            "runtime_dependency_hashes",
            "semantic_hash",
        }
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or type(document["format_version"]) is not int
            or any(
                not isinstance(document[name], str)
                for name in (
                    "entry_block",
                    "function_id",
                    "return_operation_id",
                    "semantic_hash",
                )
            )
            or not isinstance(document["blocks"], list)
            or len(document["blocks"]) > MAX_TYPED_BLOCKS
            or not isinstance(document["control_edges"], list)
            or len(document["control_edges"]) > MAX_TYPED_EDGES
            or not isinstance(document["input_types"], list)
            or len(document["input_types"]) > 256
            or not isinstance(document["operations"], list)
            or len(document["operations"]) > MAX_TYPED_OPERATIONS
            or not isinstance(document["runtime_dependency_hashes"], list)
            or len(document["runtime_dependency_hashes"])
            > MAX_RUNTIME_DEPENDENCIES
            or any(
                not isinstance(value, str)
                for value in document["runtime_dependency_hashes"]
            )
        ):
            raise ValueError("invalid typed semantic module")
        result = cls(
            document["format_version"],
            document["function_id"],
            document["entry_block"],
            tuple(TypeSpec.from_document(value) for value in document["input_types"]),
            TypeSpec.from_document(document["output_type"]),
            tuple(TypedBlock.from_document(value) for value in document["blocks"]),
            tuple(
                TypedControlEdge.from_document(value)
                for value in document["control_edges"]
            ),
            tuple(
                TypedOperation.from_document(value)
                for value in document["operations"]
            ),
            document["return_operation_id"],
            tuple(document["runtime_dependency_hashes"]),
            document["semantic_hash"],
        )
        from python_udf_jit.compiler.typed_verifier import verify_typed_module

        verify_typed_module(result)
        return result


def build_typed_module(
    *,
    function_id: str,
    entry_block: str,
    input_types: tuple[TypeSpec, ...],
    output_type: TypeSpec,
    blocks: tuple[TypedBlock, ...],
    control_edges: tuple[TypedControlEdge, ...],
    operations: tuple[TypedOperation, ...],
    return_operation_id: str,
    runtime_dependency_hashes: tuple[str, ...] = (),
) -> TypedSemanticModule:
    provisional = TypedSemanticModule(
        TYPED_SEMANTIC_IR_VERSION,
        function_id,
        entry_block,
        input_types,
        output_type,
        blocks,
        control_edges,
        operations,
        return_operation_id,
        runtime_dependency_hashes,
        "",
    )
    result = rehash_typed_module(provisional)
    from python_udf_jit.compiler.typed_verifier import verify_typed_module

    verify_typed_module(result)
    return result


def rehash_typed_module(module: TypedSemanticModule) -> TypedSemanticModule:
    provisional = replace(module, semantic_hash="")
    return replace(
        provisional,
        semantic_hash=provisional.recompute_semantic_hash(),
    )
