"""Diagnostics-only bridge for structured CinderX compilation evidence.

The normal runtime never imports this module.  Full diagnostic workers call
the optional CinderX API explicitly and receive validated, value-free records;
text dumps are not parsed as an ABI.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from python_udf_jit.diagnostics.provenance import (
    PROVENANCE_MAP_VERSION,
    ProvenanceEdge,
    ProvenanceLayer,
    ProvenanceMap,
    ProvenanceNode,
    ProvenanceRelation,
    verify_provenance_map,
)


CINDERX_DIAGNOSTICS_SCHEMA_VERSION = 1
_MAX_HIR_NODES = 131_072
_MAX_LIR_NODES = 262_144
_MAX_MACHINE_RANGES = 524_288
_MAX_PASS_TIMINGS = 4096
_MAX_DEOPT_RECORDS = 65_536
_MAX_ORIGINS_PER_NODE = 4096
_MAX_CODE_SIZE = 1 << 30
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LOCAL_ID = re.compile(r"[0-9]{1,20}")
_OPCODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}")
_PASS_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_SECTIONS = frozenset({"hot", "cold", "stub", "data"})


class CinderXDiagnosticStatus(StrEnum):
    AVAILABLE = "available"
    NOT_COMPILED = "not_compiled"
    BACKEND_UNAVAILABLE = "backend_unavailable"


def _strict_document(
    document: object,
    expected: set[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError(f"invalid CinderX {name} fields")
    return document


def _checked_text(
    value: object,
    pattern: re.Pattern[str],
    name: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid CinderX {name}")
    return value


def _checked_optional_reason(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _checked_text(value, _REASON, name)


def _checked_nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid CinderX {name}")
    return value


def _checked_local_ids(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_ORIGINS_PER_NODE
    ):
        raise ValueError(f"invalid CinderX {name}")
    result = tuple(
        _checked_text(item, _LOCAL_ID, name)
        for item in value
    )
    if len(result) != len(set(result)):
        raise ValueError(f"duplicate CinderX {name}")
    return result


@dataclass(frozen=True)
class CinderXPassTiming:
    name: str
    ordinal: int
    duration_ns: int

    @classmethod
    def from_document(cls, document: object) -> "CinderXPassTiming":
        value = _strict_document(
            document,
            {"duration_ns", "name", "ordinal"},
            "pass timing",
        )
        return cls(
            _checked_text(value["name"], _PASS_NAME, "pass name"),
            _checked_nonnegative(value["ordinal"], "pass ordinal"),
            _checked_nonnegative(value["duration_ns"], "pass duration"),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "duration_ns": self.duration_ns,
            "name": self.name,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class CinderXHIRNode:
    hir_id: str
    opcode: str
    bytecode_offset: int | None
    synthetic_kind: str | None

    @classmethod
    def from_document(cls, document: object) -> "CinderXHIRNode":
        value = _strict_document(
            document,
            {
                "bytecode_offset",
                "hir_id",
                "opcode",
                "synthetic_kind",
            },
            "HIR node",
        )
        raw_offset = value["bytecode_offset"]
        offset = (
            None
            if raw_offset is None
            else _checked_nonnegative(raw_offset, "HIR bytecode offset")
        )
        if offset is not None and offset % 2:
            raise ValueError("invalid CinderX HIR bytecode offset")
        synthetic = _checked_optional_reason(
            value["synthetic_kind"],
            "HIR synthetic kind",
        )
        if offset is None and synthetic is None:
            raise ValueError("CinderX HIR node has no origin classification")
        return cls(
            _checked_text(value["hir_id"], _LOCAL_ID, "HIR id"),
            _checked_text(value["opcode"], _OPCODE, "HIR opcode"),
            offset,
            synthetic,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "bytecode_offset": self.bytecode_offset,
            "hir_id": self.hir_id,
            "opcode": self.opcode,
            "synthetic_kind": self.synthetic_kind,
        }


@dataclass(frozen=True)
class CinderXLIRNode:
    lir_id: str
    opcode: str
    hir_ids: tuple[str, ...]
    synthetic_kind: str | None

    @classmethod
    def from_document(cls, document: object) -> "CinderXLIRNode":
        value = _strict_document(
            document,
            {"hir_ids", "lir_id", "opcode", "synthetic_kind"},
            "LIR node",
        )
        hir_ids = _checked_local_ids(value["hir_ids"], "LIR HIR origins")
        synthetic = _checked_optional_reason(
            value["synthetic_kind"],
            "LIR synthetic kind",
        )
        if not hir_ids and synthetic is None:
            raise ValueError("CinderX LIR node has no origin classification")
        return cls(
            _checked_text(value["lir_id"], _LOCAL_ID, "LIR id"),
            _checked_text(value["opcode"], _OPCODE, "LIR opcode"),
            hir_ids,
            synthetic,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "hir_ids": list(self.hir_ids),
            "lir_id": self.lir_id,
            "opcode": self.opcode,
            "synthetic_kind": self.synthetic_kind,
        }


@dataclass(frozen=True)
class CinderXMachineRange:
    range_id: str
    start: int
    end: int
    section: str
    symbol_sha256: str
    lir_ids: tuple[str, ...]
    hir_ids: tuple[str, ...]
    synthetic_kind: str | None

    @classmethod
    def from_document(cls, document: object) -> "CinderXMachineRange":
        common = {
            "end",
            "hir_ids",
            "lir_ids",
            "range_id",
            "section",
            "start",
            "synthetic_kind",
        }
        if not isinstance(document, dict):
            raise ValueError("invalid CinderX machine range fields")
        fields = set(document)
        if fields == common | {"symbol"}:
            raw_symbol = document["symbol"]
            if (
                not isinstance(raw_symbol, str)
                or not raw_symbol
                or len(raw_symbol.encode("utf-8")) > 4096
                or any(ord(character) < 0x20 for character in raw_symbol)
            ):
                raise ValueError("invalid CinderX machine symbol")
            symbol_sha256 = hashlib.sha256(
                raw_symbol.encode("utf-8")
            ).hexdigest()
        elif fields == common | {"symbol_sha256"}:
            symbol_sha256 = _checked_text(
                document["symbol_sha256"],
                _SHA256,
                "machine symbol hash",
            )
        else:
            raise ValueError("invalid CinderX machine range fields")
        start = _checked_nonnegative(document["start"], "machine range start")
        end = _checked_nonnegative(document["end"], "machine range end")
        if not start < end < 1 << 64:
            raise ValueError("invalid CinderX machine range bounds")
        section = document["section"]
        if section not in _SECTIONS:
            raise ValueError("invalid CinderX machine section")
        lir_ids = _checked_local_ids(
            document["lir_ids"],
            "machine LIR origins",
        )
        hir_ids = _checked_local_ids(
            document["hir_ids"],
            "machine HIR origins",
        )
        synthetic = _checked_optional_reason(
            document["synthetic_kind"],
            "machine synthetic kind",
        )
        if not lir_ids and not hir_ids and synthetic is None:
            raise ValueError("CinderX machine range has no classification")
        return cls(
            _checked_text(document["range_id"], _LOCAL_ID, "range id"),
            start,
            end,
            section,  # type: ignore[arg-type]
            symbol_sha256,
            lir_ids,
            hir_ids,
            synthetic,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "end": self.end,
            "hir_ids": list(self.hir_ids),
            "lir_ids": list(self.lir_ids),
            "range_id": self.range_id,
            "section": self.section,
            "start": self.start,
            "symbol_sha256": self.symbol_sha256,
            "synthetic_kind": self.synthetic_kind,
        }


@dataclass(frozen=True)
class CinderXDeoptMetadata:
    bytecode_offset: int
    reason_code: str

    @classmethod
    def from_document(cls, document: object) -> "CinderXDeoptMetadata":
        value = _strict_document(
            document,
            {"bytecode_offset", "reason_code"},
            "deopt metadata",
        )
        offset = _checked_nonnegative(
            value["bytecode_offset"],
            "deopt bytecode offset",
        )
        if offset % 2:
            raise ValueError("invalid CinderX deopt bytecode offset")
        return cls(
            offset,
            _checked_text(value["reason_code"], _REASON, "deopt reason"),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "bytecode_offset": self.bytecode_offset,
            "reason_code": self.reason_code,
        }


_TOP_LEVEL_FIELDS = {
    "code_size",
    "code_start",
    "compile_instance_id",
    "deopt_metadata",
    "generated_code_hash",
    "hir_nodes",
    "jit_compiled",
    "jit_gate_reason",
    "lir_nodes",
    "machine_ranges",
    "pass_timings",
    "schema_version",
    "spill_stack_size",
    "stack_size",
    "status",
    "unavailable_reason",
}


@dataclass(frozen=True)
class CinderXCompilationDiagnostics:
    schema_version: int
    status: CinderXDiagnosticStatus
    compile_instance_id: str
    generated_code_hash: str
    jit_compiled: bool
    unavailable_reason: str | None
    jit_gate_reason: str | None
    code_start: int | None
    code_size: int
    stack_size: int
    spill_stack_size: int
    pass_timings: tuple[CinderXPassTiming, ...]
    hir_nodes: tuple[CinderXHIRNode, ...]
    lir_nodes: tuple[CinderXLIRNode, ...]
    machine_ranges: tuple[CinderXMachineRange, ...]
    deopt_metadata: tuple[CinderXDeoptMetadata, ...]

    @classmethod
    def unavailable(
        cls,
        *,
        compile_instance_id: str,
        generated_code_hash: str,
        reason: str,
    ) -> "CinderXCompilationDiagnostics":
        return cls(
            CINDERX_DIAGNOSTICS_SCHEMA_VERSION,
            CinderXDiagnosticStatus.BACKEND_UNAVAILABLE,
            _checked_text(
                compile_instance_id,
                _IDENTITY,
                "compile instance id",
            ),
            _checked_text(
                generated_code_hash,
                _SHA256,
                "generated code hash",
            ),
            False,
            _checked_text(reason, _REASON, "unavailable reason"),
            None,
            None,
            0,
            0,
            0,
            (),
            (),
            (),
            (),
            (),
        )

    @classmethod
    def from_document(
        cls,
        document: object,
    ) -> "CinderXCompilationDiagnostics":
        value = _strict_document(
            document,
            _TOP_LEVEL_FIELDS,
            "diagnostics",
        )
        if value["schema_version"] != CINDERX_DIAGNOSTICS_SCHEMA_VERSION:
            raise ValueError("unsupported CinderX diagnostics version")
        status = CinderXDiagnosticStatus(value["status"])  # type: ignore[arg-type]
        if type(value["jit_compiled"]) is not bool:
            raise ValueError("invalid CinderX compiled flag")
        sequences: dict[str, tuple[object, ...]] = {}
        limits = {
            "pass_timings": _MAX_PASS_TIMINGS,
            "hir_nodes": _MAX_HIR_NODES,
            "lir_nodes": _MAX_LIR_NODES,
            "machine_ranges": _MAX_MACHINE_RANGES,
            "deopt_metadata": _MAX_DEOPT_RECORDS,
        }
        for name, limit in limits.items():
            raw = value[name]
            if not isinstance(raw, list) or len(raw) > limit:
                raise ValueError(f"invalid CinderX {name}")
            sequences[name] = tuple(raw)
        result = cls(
            CINDERX_DIAGNOSTICS_SCHEMA_VERSION,
            status,
            _checked_text(
                value["compile_instance_id"],
                _IDENTITY,
                "compile instance id",
            ),
            _checked_text(
                value["generated_code_hash"],
                _SHA256,
                "generated code hash",
            ),
            value["jit_compiled"],  # type: ignore[arg-type]
            _checked_optional_reason(
                value["unavailable_reason"],
                "unavailable reason",
            ),
            _checked_optional_reason(
                value["jit_gate_reason"],
                "JIT gate reason",
            ),
            (
                None
                if value["code_start"] is None
                else _checked_nonnegative(value["code_start"], "code start")
            ),
            _checked_nonnegative(value["code_size"], "code size"),
            _checked_nonnegative(value["stack_size"], "stack size"),
            _checked_nonnegative(
                value["spill_stack_size"],
                "spill stack size",
            ),
            tuple(
                CinderXPassTiming.from_document(item)
                for item in sequences["pass_timings"]
            ),
            tuple(
                CinderXHIRNode.from_document(item)
                for item in sequences["hir_nodes"]
            ),
            tuple(
                CinderXLIRNode.from_document(item)
                for item in sequences["lir_nodes"]
            ),
            tuple(
                CinderXMachineRange.from_document(item)
                for item in sequences["machine_ranges"]
            ),
            tuple(
                CinderXDeoptMetadata.from_document(item)
                for item in sequences["deopt_metadata"]
            ),
        )
        _verify_compilation_diagnostics(result)
        return result

    def to_document(self) -> dict[str, object]:
        _verify_compilation_diagnostics(self)
        return {
            "code_size": self.code_size,
            "code_start": self.code_start,
            "compile_instance_id": self.compile_instance_id,
            "deopt_metadata": [
                item.to_document() for item in self.deopt_metadata
            ],
            "generated_code_hash": self.generated_code_hash,
            "hir_nodes": [item.to_document() for item in self.hir_nodes],
            "jit_compiled": self.jit_compiled,
            "jit_gate_reason": self.jit_gate_reason,
            "lir_nodes": [item.to_document() for item in self.lir_nodes],
            "machine_ranges": [
                item.to_document() for item in self.machine_ranges
            ],
            "pass_timings": [
                item.to_document() for item in self.pass_timings
            ],
            "schema_version": self.schema_version,
            "spill_stack_size": self.spill_stack_size,
            "stack_size": self.stack_size,
            "status": self.status.value,
            "unavailable_reason": self.unavailable_reason,
        }


def _verify_unique(values: tuple[str, ...], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate CinderX {name}")


def _verify_compilation_diagnostics(
    diagnostics: CinderXCompilationDiagnostics,
) -> None:
    if diagnostics.schema_version != CINDERX_DIAGNOSTICS_SCHEMA_VERSION:
        raise ValueError("unsupported CinderX diagnostics version")
    _checked_text(
        diagnostics.compile_instance_id,
        _IDENTITY,
        "compile instance id",
    )
    _checked_text(
        diagnostics.generated_code_hash,
        _SHA256,
        "generated code hash",
    )
    if diagnostics.code_size > _MAX_CODE_SIZE:
        raise ValueError("CinderX code size limit")
    if diagnostics.spill_stack_size > diagnostics.stack_size:
        raise ValueError("CinderX spill stack exceeds stack size")
    populated = (
        diagnostics.pass_timings
        or diagnostics.hir_nodes
        or diagnostics.lir_nodes
        or diagnostics.machine_ranges
        or diagnostics.deopt_metadata
    )
    if diagnostics.status is CinderXDiagnosticStatus.AVAILABLE:
        if (
            not diagnostics.jit_compiled
            or diagnostics.unavailable_reason is not None
            or diagnostics.jit_gate_reason is not None
            or diagnostics.code_start is None
            or diagnostics.code_start <= 0
            or diagnostics.code_size <= 0
            or diagnostics.code_start + diagnostics.code_size >= 1 << 64
            or not diagnostics.hir_nodes
            or not diagnostics.lir_nodes
            or not diagnostics.machine_ranges
        ):
            raise ValueError("inconsistent available CinderX diagnostics")
    elif diagnostics.status is CinderXDiagnosticStatus.NOT_COMPILED:
        if (
            diagnostics.jit_compiled
            or diagnostics.jit_gate_reason is None
            or diagnostics.unavailable_reason is not None
            or diagnostics.code_start is not None
            or diagnostics.code_size
            or diagnostics.stack_size
            or diagnostics.spill_stack_size
            or populated
        ):
            raise ValueError("inconsistent CinderX not-compiled diagnostics")
        return
    elif (
        diagnostics.jit_compiled
        or diagnostics.unavailable_reason is None
        or diagnostics.jit_gate_reason is not None
        or diagnostics.code_start is not None
        or diagnostics.code_size
        or diagnostics.stack_size
        or diagnostics.spill_stack_size
        or populated
    ):
        raise ValueError("inconsistent unavailable CinderX diagnostics")
    else:
        return

    timing_keys = tuple(
        (timing.name, timing.ordinal)
        for timing in diagnostics.pass_timings
    )
    if len(timing_keys) != len(set(timing_keys)):
        raise ValueError("duplicate CinderX pass timing")
    hir_ids = tuple(node.hir_id for node in diagnostics.hir_nodes)
    lir_ids = tuple(node.lir_id for node in diagnostics.lir_nodes)
    range_ids = tuple(item.range_id for item in diagnostics.machine_ranges)
    _verify_unique(hir_ids, "HIR id")
    _verify_unique(lir_ids, "LIR id")
    _verify_unique(range_ids, "range id")
    hir_id_set = set(hir_ids)
    lir_id_set = set(lir_ids)
    for node in diagnostics.lir_nodes:
        if any(origin not in hir_id_set for origin in node.hir_ids):
            raise ValueError("dangling CinderX LIR HIR origin")
    assert diagnostics.code_start is not None
    code_end = diagnostics.code_start + diagnostics.code_size
    previous_end = diagnostics.code_start
    for machine_range in sorted(
        diagnostics.machine_ranges,
        key=lambda item: (item.start, item.end, item.range_id),
    ):
        if (
            machine_range.start < diagnostics.code_start
            or machine_range.end > code_end
            or machine_range.start < previous_end
        ):
            raise ValueError("invalid CinderX machine range coverage")
        previous_end = machine_range.end
        if any(origin not in hir_id_set for origin in machine_range.hir_ids):
            raise ValueError("dangling CinderX machine HIR origin")
        if any(origin not in lir_id_set for origin in machine_range.lir_ids):
            raise ValueError("dangling CinderX machine LIR origin")


def collect_cinderx_compilation_diagnostics(
    jit_module: object,
    function: Callable[..., object],
    *,
    compile_instance_id: str,
    generated_code_hash: str,
) -> CinderXCompilationDiagnostics:
    """Collect the optional structured API without parsing text fallbacks."""

    compile_instance_id = _checked_text(
        compile_instance_id,
        _IDENTITY,
        "compile instance id",
    )
    generated_code_hash = _checked_text(
        generated_code_hash,
        _SHA256,
        "generated code hash",
    )
    api = getattr(jit_module, "get_udfjit_compilation_diagnostics", None)
    if not callable(api):
        return CinderXCompilationDiagnostics.unavailable(
            compile_instance_id=compile_instance_id,
            generated_code_hash=generated_code_hash,
            reason="structured_api_unavailable",
        )
    try:
        document = api(function, compile_instance_id)
    except Exception:
        return CinderXCompilationDiagnostics.unavailable(
            compile_instance_id=compile_instance_id,
            generated_code_hash=generated_code_hash,
            reason="structured_api_error",
        )
    diagnostics = CinderXCompilationDiagnostics.from_document(document)
    if (
        diagnostics.compile_instance_id != compile_instance_id
        or diagnostics.generated_code_hash != generated_code_hash
    ):
        raise ValueError("CinderX diagnostics identity mismatch")
    return diagnostics


def _attrs(*items: tuple[str, str | None]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, value) for key, value in items if value is not None))


def extend_provenance_with_cinderx(
    provenance: ProvenanceMap,
    diagnostics: CinderXCompilationDiagnostics,
) -> ProvenanceMap:
    """Append Generated Bytecode -> HIR -> LIR -> Machine provenance."""

    verify_provenance_map(provenance)
    _verify_compilation_diagnostics(diagnostics)
    if diagnostics.status is not CinderXDiagnosticStatus.AVAILABLE:
        return provenance
    generated_by_offset = {
        node.bytecode_offset: node.node_id
        for node in provenance.nodes
        if (
            node.layer is ProvenanceLayer.GENERATED_BYTECODE
            and node.node_id.startswith(
                f"genbc:{diagnostics.generated_code_hash}:"
            )
        )
    }
    hir_ids = {
        node.hir_id: (
            f"hir:{diagnostics.compile_instance_id}:{node.hir_id}"
        )
        for node in diagnostics.hir_nodes
    }
    lir_ids = {
        node.lir_id: (
            f"lir:{diagnostics.compile_instance_id}:{node.lir_id}"
        )
        for node in diagnostics.lir_nodes
    }
    nodes = list(provenance.nodes)
    edges = list(provenance.edges)
    for node in diagnostics.hir_nodes:
        full_id = hir_ids[node.hir_id]
        nodes.append(
            ProvenanceNode(
                full_id,
                ProvenanceLayer.HIR,
                node.opcode,
                attributes=_attrs(
                    (
                        "bytecode_offset",
                        (
                            None
                            if node.bytecode_offset is None
                            else str(node.bytecode_offset)
                        ),
                    ),
                    ("synthetic_kind", node.synthetic_kind),
                ),
            )
        )
        if node.bytecode_offset is not None:
            generated = generated_by_offset.get(node.bytecode_offset)
            if generated is None:
                raise ValueError(
                    "CinderX HIR references unknown generated bytecode"
                )
            edges.append(
                ProvenanceEdge(
                    generated,
                    full_id,
                    ProvenanceRelation.LOWERED,
                    pass_name="cinderx_frontend",
                )
            )
    for node in diagnostics.lir_nodes:
        full_id = lir_ids[node.lir_id]
        nodes.append(
            ProvenanceNode(
                full_id,
                ProvenanceLayer.LIR,
                node.opcode,
                attributes=_attrs(
                    ("synthetic_kind", node.synthetic_kind),
                ),
            )
        )
        for origin in node.hir_ids:
            edges.append(
                ProvenanceEdge(
                    hir_ids[origin],
                    full_id,
                    (
                        ProvenanceRelation.FUSED
                        if len(node.hir_ids) > 1
                        else ProvenanceRelation.LOWERED
                    ),
                    pass_name="cinderx_lir_lowering",
                )
            )
    for machine_range in diagnostics.machine_ranges:
        full_id = (
            f"machine:{diagnostics.compile_instance_id}:"
            f"{machine_range.range_id}"
        )
        nodes.append(
            ProvenanceNode(
                full_id,
                ProvenanceLayer.MACHINE,
                "machine_range",
                attributes=_attrs(
                    ("section", machine_range.section),
                    ("symbol_sha256", machine_range.symbol_sha256),
                    ("synthetic_kind", machine_range.synthetic_kind),
                ),
                address_start=machine_range.start,
                address_end=machine_range.end,
            )
        )
        origins = [
            *(lir_ids[value] for value in machine_range.lir_ids),
            *(hir_ids[value] for value in machine_range.hir_ids),
        ]
        for origin in origins:
            edges.append(
                ProvenanceEdge(
                    origin,
                    full_id,
                    (
                        ProvenanceRelation.FUSED
                        if len(origins) > 1
                        else ProvenanceRelation.LOWERED
                    ),
                    pass_name="cinderx_codegen",
                )
            )
    result = ProvenanceMap(
        PROVENANCE_MAP_VERSION,
        tuple(nodes),
        tuple(edges),
    )
    verify_provenance_map(result)
    return result


@dataclass(frozen=True)
class CinderXArtifacts:
    hir_json: dict[str, object]
    hir_text: str
    lir_json: dict[str, object]
    lir_text: str
    machine_ranges_json: dict[str, object]
    machine_ranges_text: str
    compile_stats_json: dict[str, object]


def build_cinderx_artifacts(
    diagnostics: CinderXCompilationDiagnostics,
) -> CinderXArtifacts:
    """Render structural, literal-free HIR/LIR/range artifacts."""

    _verify_compilation_diagnostics(diagnostics)
    hir_lines = [
        (
            f"hir:{diagnostics.compile_instance_id}:{node.hir_id}"
            f" {node.opcode}"
            f" bytecode_offset={node.bytecode_offset!s}"
            f" synthetic={node.synthetic_kind or '-'}"
        )
        for node in diagnostics.hir_nodes
    ]
    lir_lines = [
        (
            f"lir:{diagnostics.compile_instance_id}:{node.lir_id}"
            f" {node.opcode}"
            f" hir={','.join(node.hir_ids) or '-'}"
            f" synthetic={node.synthetic_kind or '-'}"
        )
        for node in diagnostics.lir_nodes
    ]
    range_lines = [
        (
            f"machine:{diagnostics.compile_instance_id}:{item.range_id}"
            f" [{item.start:#x},{item.end:#x})"
            f" section={item.section}"
            f" lir={','.join(item.lir_ids) or '-'}"
            f" hir={','.join(item.hir_ids) or '-'}"
            f" synthetic={item.synthetic_kind or '-'}"
        )
        for item in diagnostics.machine_ranges
    ]
    return CinderXArtifacts(
        {
            "nodes": [item.to_document() for item in diagnostics.hir_nodes],
            "schema_version": diagnostics.schema_version,
        },
        "\n".join(hir_lines) + ("\n" if hir_lines else ""),
        {
            "nodes": [item.to_document() for item in diagnostics.lir_nodes],
            "schema_version": diagnostics.schema_version,
        },
        "\n".join(lir_lines) + ("\n" if lir_lines else ""),
        {
            "code_size": diagnostics.code_size,
            "code_start": diagnostics.code_start,
            "ranges": [
                item.to_document() for item in diagnostics.machine_ranges
            ],
            "schema_version": diagnostics.schema_version,
        },
        "\n".join(range_lines) + ("\n" if range_lines else ""),
        {
            "compile_instance_id": diagnostics.compile_instance_id,
            "deopt_metadata": [
                item.to_document() for item in diagnostics.deopt_metadata
            ],
            "generated_code_hash": diagnostics.generated_code_hash,
            "jit_compiled": diagnostics.jit_compiled,
            "jit_gate_reason": diagnostics.jit_gate_reason,
            "pass_timings": [
                item.to_document() for item in diagnostics.pass_timings
            ],
            "schema_version": diagnostics.schema_version,
            "spill_stack_size": diagnostics.spill_stack_size,
            "stack_size": diagnostics.stack_size,
            "status": diagnostics.status.value,
            "unavailable_reason": diagnostics.unavailable_reason,
        },
    )
