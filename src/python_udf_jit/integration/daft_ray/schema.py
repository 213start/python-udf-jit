from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


class SchemaContractError(ValueError):
    """A framework schema could not be reduced to the scalar logical contract."""


def _field_name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def canonicalize_logical_type(value: Any) -> str:
    value_type = type(value)
    if (
        isinstance(value, str)
        or (
            value_type.__module__.startswith("daft")
            and value_type.__name__.endswith("DataType")
        )
    ):
        candidate = value
    else:
        candidate = getattr(value, "dtype", value)
    if isinstance(candidate, str):
        logical_type = candidate
    elif type(candidate).__module__.startswith("daft"):
        logical_type = str(candidate)
    else:
        raise SchemaContractError("schema_type_unsupported")
    logical_type = logical_type.strip().lower()
    if not logical_type or len(logical_type.encode("utf-8")) > 256:
        raise SchemaContractError("schema_type_invalid")
    return {
        "boolean": "bool",
        "utf8": "string",
    }.get(logical_type, logical_type)


def _schema_items(schema: Any) -> tuple[tuple[str, Any], ...]:
    if isinstance(schema, Mapping):
        return tuple((str(name), value) for name, value in schema.items())
    column_names = getattr(schema, "column_names", None)
    if not callable(column_names):
        raise SchemaContractError("schema_interface_unsupported")
    try:
        names = tuple(column_names())
        return tuple((str(name), schema[name]) for name in names)
    except Exception as error:
        raise SchemaContractError("schema_read_failed") from error


def canonicalize_schema(schema: Any) -> str:
    """Return a deterministic, value-free full-schema diagnostic document."""

    fields = [
        {
            "name_sha256": _field_name_hash(name),
            "logical_type": canonicalize_logical_type(value),
        }
        for name, value in _schema_items(schema)
    ]
    fields.sort(key=lambda field: field["name_sha256"])
    if len(fields) > 4096:
        raise SchemaContractError("schema_field_limit")
    return json.dumps(
        {"schema_version": 1, "fields": fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
