from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from python_udf_jit.integration.daft_ray.schema import (
    SchemaContractError,
    canonicalize_logical_type,
)
from python_udf_jit.runtime.layout import (
    SCALAR_LAYOUT_KIND,
    SCALAR_SLOT_ABI_VERSION,
    SUPPORTED_SCALAR_TYPES,
)


INVOCATION_LAYOUT_SCHEMA_VERSION = 1
EXACT_UNICODE_LAYOUT_KIND = "exact_unicode"
SCALAR_SLOT_LAYOUT_KIND = SCALAR_LAYOUT_KIND
PYTHON_OBJECT_LAYOUT_KIND = "python_object"
_LAYOUT_KINDS = {
    EXACT_UNICODE_LAYOUT_KIND,
    SCALAR_SLOT_LAYOUT_KIND,
    PYTHON_OBJECT_LAYOUT_KIND,
}
_EXACT_UNICODE_OUTPUT_TYPES = {
    "bool",
    "int64",
    "float64",
    "string",
}


class InvocationLayoutError(ValueError):
    """One candidate invocation cannot be reduced to a guarded scalar layout."""


@dataclass(frozen=True)
class InvocationLayoutContract:
    """Address-free, candidate-local physical layout binding.

    The contract deliberately contains no DataFrame field names or unrelated
    column types.  It describes only the logical values that cross one UDF call
    boundary and the process-generation epoch in which its Worker layout may be
    bound.
    """

    schema_version: int
    input_types: tuple[str, ...]
    output_type: str
    input_nullability: tuple[bool, ...]
    output_nullable: bool
    layout_kind: str
    layout_abi_version: int
    epoch: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != INVOCATION_LAYOUT_SCHEMA_VERSION
            or not self.input_types
            or any(type(value) is not str or not value for value in self.input_types)
            or type(self.output_type) is not str
            or not self.output_type
            or len(self.input_nullability) != len(self.input_types)
            or any(type(value) is not bool for value in self.input_nullability)
            or type(self.output_nullable) is not bool
            or self.layout_kind not in _LAYOUT_KINDS
            or type(self.layout_abi_version) is not int
            or self.layout_abi_version <= 0
            or type(self.epoch) is not str
            or not self.epoch
        ):
            raise InvocationLayoutError("invocation_layout_invalid")
        if (
            self.layout_kind == SCALAR_SLOT_LAYOUT_KIND
            and (
                len(self.input_types) != 1
                or self.input_types[0] not in SUPPORTED_SCALAR_TYPES
                or self.output_type not in SUPPORTED_SCALAR_TYPES
                or self.layout_abi_version != SCALAR_SLOT_ABI_VERSION
            )
        ):
            raise InvocationLayoutError("scalar_slot_layout_invalid")
        if (
            self.layout_kind == EXACT_UNICODE_LAYOUT_KIND
            and (
                self.input_types != ("string",)
                or self.output_type not in _EXACT_UNICODE_OUTPUT_TYPES
                or self.layout_abi_version != 1
            )
        ):
            raise InvocationLayoutError("exact_unicode_layout_invalid")
        if (
            self.layout_kind == PYTHON_OBJECT_LAYOUT_KIND
            and self.layout_abi_version != 1
        ):
            raise InvocationLayoutError("python_object_layout_invalid")

    @classmethod
    def for_types(
        cls,
        input_types: tuple[str, ...],
        output_type: str,
        *,
        epoch: str,
        input_nullability: tuple[bool, ...] | None = None,
        output_nullable: bool = False,
    ) -> "InvocationLayoutContract":
        normalized_inputs = tuple(
            canonicalize_logical_type(value) for value in input_types
        )
        normalized_output = canonicalize_logical_type(output_type)
        nullability = (
            (False,) * len(normalized_inputs)
            if input_nullability is None
            else input_nullability
        )
        if (
            len(normalized_inputs) == 1
            and normalized_inputs[0] in SUPPORTED_SCALAR_TYPES
            and normalized_output in SUPPORTED_SCALAR_TYPES
        ):
            layout_kind = SCALAR_SLOT_LAYOUT_KIND
            abi_version = SCALAR_SLOT_ABI_VERSION
        elif (
            normalized_inputs == ("string",)
            and normalized_output in _EXACT_UNICODE_OUTPUT_TYPES
        ):
            layout_kind = EXACT_UNICODE_LAYOUT_KIND
            abi_version = 1
        else:
            layout_kind = PYTHON_OBJECT_LAYOUT_KIND
            abi_version = 1
        return cls(
            INVOCATION_LAYOUT_SCHEMA_VERSION,
            normalized_inputs,
            normalized_output,
            tuple(nullability),
            output_nullable,
            layout_kind,
            abi_version,
            epoch,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "input_nullability": list(self.input_nullability),
            "input_types": list(self.input_types),
            "layout_abi_version": self.layout_abi_version,
            "layout_kind": self.layout_kind,
            "output_nullable": self.output_nullable,
            "output_type": self.output_type,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_document(cls, document: object) -> "InvocationLayoutContract":
        expected = {
            "epoch",
            "input_nullability",
            "input_types",
            "layout_abi_version",
            "layout_kind",
            "output_nullable",
            "output_type",
            "schema_version",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise InvocationLayoutError("invocation_layout_fields_invalid")
        input_types = document["input_types"]
        input_nullability = document["input_nullability"]
        if not isinstance(input_types, list) or not isinstance(
            input_nullability, list
        ):
            raise InvocationLayoutError("invocation_layout_sequences_invalid")
        return cls(
            document["schema_version"],  # type: ignore[arg-type]
            tuple(input_types),  # type: ignore[arg-type]
            document["output_type"],  # type: ignore[arg-type]
            tuple(input_nullability),  # type: ignore[arg-type]
            document["output_nullable"],  # type: ignore[arg-type]
            document["layout_kind"],  # type: ignore[arg-type]
            document["layout_abi_version"],  # type: ignore[arg-type]
            document["epoch"],  # type: ignore[arg-type]
        )

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.to_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()


def _framework_schema(schema: Any) -> Any:
    try:
        namespace = object.__getattribute__(schema, "__dict__")
    except (AttributeError, TypeError):
        return schema
    if type(namespace) is not dict:
        return schema
    return namespace.get("_schema", schema)


def resolve_expression_logical_type(value: Any, schema: Any) -> str:
    """Resolve one expression against the DataFrame schema that will execute it."""

    try:
        namespace = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError) as error:
        raise InvocationLayoutError("candidate_input_expression_required") from error
    if type(namespace) is not dict:
        raise InvocationLayoutError("candidate_input_expression_required")
    pyexpr = namespace.get("_expr")
    to_field = getattr(pyexpr, "to_field", None)
    if not callable(to_field):
        raise InvocationLayoutError("candidate_input_type_unavailable")
    try:
        field = to_field(_framework_schema(schema))
        dtype = field.dtype()
    except Exception as error:
        raise InvocationLayoutError("candidate_input_type_resolution_failed") from error
    try:
        return canonicalize_logical_type(dtype)
    except SchemaContractError as error:
        raise InvocationLayoutError("candidate_input_type_unsupported") from error


def _return_logical_type(func: Any) -> str:
    try:
        return_dtype = object.__getattribute__(func, "return_dtype")
    except (AttributeError, TypeError) as error:
        raise InvocationLayoutError("candidate_output_type_unavailable") from error
    if return_dtype is None:
        raise InvocationLayoutError("candidate_output_type_unavailable")
    try:
        return canonicalize_logical_type(return_dtype)
    except SchemaContractError as error:
        raise InvocationLayoutError("candidate_output_type_unsupported") from error


def build_invocation_layout(
    func: Any,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    schema: Any,
    *,
    epoch: str,
) -> InvocationLayoutContract:
    """Resolve one UDF's layout from its own expression inputs and return type."""

    if not isinstance(kwargs, Mapping):
        raise InvocationLayoutError("candidate_keyword_inputs_invalid")
    values = (*args, *kwargs.values())
    if not values:
        raise InvocationLayoutError("candidate_input_missing")
    input_types = tuple(
        resolve_expression_logical_type(value, schema) for value in values
    )
    return InvocationLayoutContract.for_types(
        input_types,
        _return_logical_type(func),
        epoch=epoch,
    )
