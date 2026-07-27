from __future__ import annotations

import json
import types
from dataclasses import dataclass
from typing import Any, Iterable


SOURCE_MAP_VERSION = 1


class SourceMapError(ValueError):
    """Raised when a CPython location table cannot be decoded safely."""


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class SourcePosition:
    line: int | None
    end_line: int | None
    column: int | None
    end_column: int | None

    def to_document(self) -> dict[str, int | None]:
        return {
            "column": self.column,
            "end_column": self.end_column,
            "end_line": self.end_line,
            "line": self.line,
        }

    @classmethod
    def from_document(cls, document: object) -> "SourcePosition":
        expected = {"column", "end_column", "end_line", "line"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid source position fields")
        values = (
            document["line"],
            document["end_line"],
            document["column"],
            document["end_column"],
        )
        if any(value is not None and type(value) is not int for value in values):
            raise ValueError("invalid source position value")
        position = cls(*values)
        _verify_position(position)
        return position


@dataclass(frozen=True)
class SourceMapEntry:
    bytecode_offset: int
    position: SourcePosition

    def to_document(self) -> dict[str, Any]:
        return {
            "bytecode_offset": self.bytecode_offset,
            "position": self.position.to_document(),
        }

    @classmethod
    def from_document(cls, document: object) -> "SourceMapEntry":
        expected = {"bytecode_offset", "position"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid source map entry fields")
        if type(document["bytecode_offset"]) is not int:
            raise ValueError("invalid source map offset")
        return cls(
            document["bytecode_offset"],
            SourcePosition.from_document(document["position"]),
        )


@dataclass(frozen=True)
class SourceMap:
    format_version: int
    entries: tuple[SourceMapEntry, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_document() for entry in self.entries],
            "format_version": self.format_version,
        }

    def canonical_bytes(self) -> bytes:
        verify_source_map(self)
        return _canonical_bytes(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "SourceMap":
        expected = {"entries", "format_version"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid source map fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid source map version")
        entries = document["entries"]
        if not isinstance(entries, list):
            raise ValueError("invalid source map entries")
        source_map = cls(
            document["format_version"],
            tuple(SourceMapEntry.from_document(entry) for entry in entries),
        )
        verify_source_map(source_map)
        return source_map


def _verify_position(position: SourcePosition) -> None:
    line_values = (position.line, position.end_line)
    column_values = (position.column, position.end_column)
    if (line_values[0] is None) != (line_values[1] is None):
        raise ValueError("partial source line range")
    if (column_values[0] is None) != (column_values[1] is None):
        raise ValueError("partial source column range")
    if any(value is not None and value < 0 for value in (*line_values, *column_values)):
        raise ValueError("negative source position")
    if position.line is not None and position.end_line is not None:
        if position.end_line < position.line:
            raise ValueError("reversed source line range")
        if (
            position.end_line == position.line
            and position.column is not None
            and position.end_column is not None
            and position.end_column < position.column
        ):
            raise ValueError("reversed source column range")


def verify_source_map(
    source_map: SourceMap,
    *,
    expected_offsets: Iterable[int] | None = None,
) -> None:
    if source_map.format_version != SOURCE_MAP_VERSION:
        raise ValueError("unsupported source map version")
    offsets: list[int] = []
    for entry in source_map.entries:
        if type(entry.bytecode_offset) is not int or entry.bytecode_offset < 0:
            raise ValueError("invalid source map offset")
        _verify_position(entry.position)
        offsets.append(entry.bytecode_offset)
    if offsets != sorted(set(offsets)):
        raise ValueError("source map offsets must be unique and sorted")
    if expected_offsets is not None and tuple(offsets) != tuple(expected_offsets):
        raise ValueError("source map does not cover decoded instructions")


def decode_source_map(
    code: types.CodeType,
    instruction_offsets: Iterable[int],
    *,
    max_code_units: int = 524_288,
) -> SourceMap:
    """Decode ``co_linetable`` without formatting source or reading a file."""

    if type(code) is not types.CodeType:
        raise SourceMapError("invalid code object")
    unit_count = len(code.co_code) // 2
    if unit_count > max_code_units:
        raise SourceMapError("location table exceeds configured limit")
    try:
        raw_positions = tuple(code.co_positions())
    except (ValueError, RuntimeError, SystemError) as error:
        raise SourceMapError("malformed CPython location table") from error
    if len(raw_positions) != unit_count:
        raise SourceMapError("location table does not cover every code unit")

    entries: list[SourceMapEntry] = []
    try:
        for offset in instruction_offsets:
            if type(offset) is not int or offset < 0 or offset % 2 or offset // 2 >= unit_count:
                raise SourceMapError("instruction offset is outside location table")
            raw = raw_positions[offset // 2]
            if not isinstance(raw, tuple) or len(raw) != 4:
                raise SourceMapError("malformed source position")
            if any(value is not None and type(value) is not int for value in raw):
                raise SourceMapError("non-integer source position")
            position = SourcePosition(raw[0], raw[1], raw[2], raw[3])
            _verify_position(position)
            entries.append(SourceMapEntry(offset, position))
        source_map = SourceMap(SOURCE_MAP_VERSION, tuple(entries))
        verify_source_map(source_map)
        return source_map
    except ValueError as error:
        if isinstance(error, SourceMapError):
            raise
        raise SourceMapError(str(error)) from error
