"""Bounded perf sample normalization and provenance-aware hotspot projection."""
from __future__ import annotations

import bisect
import hashlib
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from python_udf_jit.diagnostics.provenance import (
    ProvenanceLayer,
    ProvenanceMap,
    verify_provenance_map,
)


PERF_PROFILE_VERSION = 1
HOTSPOT_REPORT_VERSION = 1
_MAX_SAMPLES = 2_000_000
_MAX_PERIOD = (1 << 63) - 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_EVENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_PHASE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")


class HotspotGroupBy(StrEnum):
    SOURCE = "source"
    ORIGINAL_BYTECODE = "original_bytecode"
    OPERATION = "operation"
    REGION = "region"
    GENERATED_BYTECODE = "generated_bytecode"
    HIR = "hir"
    LIR = "lir"
    MACHINE = "machine"
    SYMBOL = "symbol"
    PHASE = "phase"


class AttributionClass(StrEnum):
    EXACT = "exact"
    SHARED = "shared"
    MIXED = "mixed"


_GROUP_LAYER = {
    HotspotGroupBy.SOURCE: ProvenanceLayer.SOURCE,
    HotspotGroupBy.ORIGINAL_BYTECODE: ProvenanceLayer.ORIGINAL_BYTECODE,
    HotspotGroupBy.OPERATION: ProvenanceLayer.CORE_OPERATION,
    HotspotGroupBy.REGION: ProvenanceLayer.REGION,
    HotspotGroupBy.GENERATED_BYTECODE: ProvenanceLayer.GENERATED_BYTECODE,
    HotspotGroupBy.HIR: ProvenanceLayer.HIR,
    HotspotGroupBy.LIR: ProvenanceLayer.LIR,
    HotspotGroupBy.MACHINE: ProvenanceLayer.MACHINE,
}


def _strict(document: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError(f"invalid {name} fields")
    return document


def _text(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _positive(value: object, name: str) -> int:
    result = _nonnegative(value, name)
    if result == 0:
        raise ValueError(f"invalid {name}")
    return result


@dataclass(frozen=True)
class PerfSample:
    sample_id: str
    pid: int
    tid: int
    timestamp_ns: int
    event: str
    ip: int
    period: int
    runtime_phase: str | None
    symbol_sha256: str | None

    @classmethod
    def from_document(cls, document: object) -> "PerfSample":
        common = {
            "event",
            "ip",
            "period",
            "pid",
            "runtime_phase",
            "sample_id",
            "tid",
            "timestamp_ns",
        }
        if not isinstance(document, dict):
            raise ValueError("invalid perf sample fields")
        fields = set(document)
        if fields == common | {"symbol"}:
            raw_symbol = document["symbol"]
            if (
                not isinstance(raw_symbol, str)
                or not raw_symbol
                or len(raw_symbol.encode("utf-8")) > 4096
                or any(ord(character) < 0x20 for character in raw_symbol)
            ):
                raise ValueError("invalid perf symbol")
            symbol_sha256 = hashlib.sha256(
                raw_symbol.encode("utf-8")
            ).hexdigest()
        elif fields == common | {"symbol_sha256"}:
            raw_digest = document["symbol_sha256"]
            symbol_sha256 = (
                None
                if raw_digest is None
                else _text(raw_digest, _SHA256, "perf symbol hash")
            )
        else:
            raise ValueError("invalid perf sample fields")
        pid = _positive(document["pid"], "perf pid")
        tid = _positive(document["tid"], "perf tid")
        ip = _nonnegative(document["ip"], "perf instruction pointer")
        period = _positive(document["period"], "perf period")
        if ip >= 1 << 64 or period > _MAX_PERIOD:
            raise ValueError("perf sample scalar limit")
        raw_phase = document["runtime_phase"]
        phase = (
            None
            if raw_phase is None
            else _text(raw_phase, _PHASE, "runtime phase")
        )
        return cls(
            _text(document["sample_id"], _SAFE_ID, "sample id"),
            pid,
            tid,
            _nonnegative(document["timestamp_ns"], "perf timestamp"),
            _text(document["event"], _EVENT, "perf event"),
            ip,
            period,
            phase,
            symbol_sha256,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "event": self.event,
            "ip": self.ip,
            "period": self.period,
            "pid": self.pid,
            "runtime_phase": self.runtime_phase,
            "sample_id": self.sample_id,
            "symbol_sha256": self.symbol_sha256,
            "tid": self.tid,
            "timestamp_ns": self.timestamp_ns,
        }


@dataclass(frozen=True)
class NormalizedPerfProfile:
    schema_version: int
    run_id: str
    process_id: int
    event: str
    lost_samples: int
    samples: tuple[PerfSample, ...]

    @classmethod
    def from_document(cls, document: object) -> "NormalizedPerfProfile":
        value = _strict(
            document,
            {
                "event",
                "lost_samples",
                "process_id",
                "run_id",
                "samples",
                "schema_version",
            },
            "perf profile",
        )
        if value["schema_version"] != PERF_PROFILE_VERSION:
            raise ValueError("unsupported perf profile version")
        raw_samples = value["samples"]
        if not isinstance(raw_samples, list) or len(raw_samples) > _MAX_SAMPLES:
            raise ValueError("invalid perf samples")
        result = cls(
            PERF_PROFILE_VERSION,
            _text(value["run_id"], _SAFE_ID, "perf run id"),
            _positive(value["process_id"], "perf process id"),
            _text(value["event"], _EVENT, "perf profile event"),
            _nonnegative(value["lost_samples"], "lost samples"),
            tuple(PerfSample.from_document(item) for item in raw_samples),
        )
        if len(result.samples) != len(
            {sample.sample_id for sample in result.samples}
        ):
            raise ValueError("duplicate perf sample id")
        if any(
            sample.pid != result.process_id
            or sample.event != result.event
            for sample in result.samples
        ):
            raise ValueError("perf sample scope mismatch")
        return result

    def to_document(self) -> dict[str, object]:
        return {
            "event": self.event,
            "lost_samples": self.lost_samples,
            "process_id": self.process_id,
            "run_id": self.run_id,
            "samples": [sample.to_document() for sample in self.samples],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class HotspotEntry:
    key: str
    weight: float
    exact_weight: float
    shared_weight: float
    sample_count: int
    classification: AttributionClass

    def to_document(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "exact_weight": self.exact_weight,
            "key": self.key,
            "sample_count": self.sample_count,
            "shared_weight": self.shared_weight,
            "weight": self.weight,
        }

    @classmethod
    def from_document(cls, document: object) -> "HotspotEntry":
        value = _strict(
            document,
            {
                "classification",
                "exact_weight",
                "key",
                "sample_count",
                "shared_weight",
                "weight",
            },
            "hotspot entry",
        )
        numbers = tuple(
            _finite_weight(value[name], name)
            for name in ("weight", "exact_weight", "shared_weight")
        )
        return cls(
            _text(value["key"], _SAFE_ID, "hotspot key"),
            numbers[0],
            numbers[1],
            numbers[2],
            _nonnegative(value["sample_count"], "hotspot sample count"),
            AttributionClass(value["classification"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class HotspotReport:
    schema_version: int
    run_id: str
    event: str
    group_by: HotspotGroupBy
    total_weight: int
    attributed_weight: int
    exact_weight: int
    shared_weight: int
    unattributed_weight: int
    coverage: float
    lost_samples: int
    entries: tuple[HotspotEntry, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "attributed_weight": self.attributed_weight,
            "coverage": self.coverage,
            "entries": [entry.to_document() for entry in self.entries],
            "event": self.event,
            "exact_weight": self.exact_weight,
            "group_by": self.group_by.value,
            "lost_samples": self.lost_samples,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "shared_weight": self.shared_weight,
            "total_weight": self.total_weight,
            "unattributed_weight": self.unattributed_weight,
        }

    @classmethod
    def from_document(cls, document: object) -> "HotspotReport":
        value = _strict(
            document,
            {
                "attributed_weight",
                "coverage",
                "entries",
                "event",
                "exact_weight",
                "group_by",
                "lost_samples",
                "run_id",
                "schema_version",
                "shared_weight",
                "total_weight",
                "unattributed_weight",
            },
            "hotspot report",
        )
        if value["schema_version"] != HOTSPOT_REPORT_VERSION:
            raise ValueError("unsupported hotspot report version")
        raw_entries = value["entries"]
        if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_SAMPLES:
            raise ValueError("invalid hotspot entries")
        result = cls(
            HOTSPOT_REPORT_VERSION,
            _text(value["run_id"], _SAFE_ID, "hotspot run id"),
            _text(value["event"], _EVENT, "hotspot event"),
            HotspotGroupBy(value["group_by"]),  # type: ignore[arg-type]
            _nonnegative(value["total_weight"], "total weight"),
            _nonnegative(value["attributed_weight"], "attributed weight"),
            _nonnegative(value["exact_weight"], "exact weight"),
            _nonnegative(value["shared_weight"], "shared weight"),
            _nonnegative(value["unattributed_weight"], "unattributed weight"),
            _finite_weight(value["coverage"], "coverage"),
            _nonnegative(value["lost_samples"], "lost samples"),
            tuple(HotspotEntry.from_document(item) for item in raw_entries),
        )
        if (
            result.attributed_weight
            != result.exact_weight + result.shared_weight
            or result.total_weight
            != result.attributed_weight + result.unattributed_weight
            or not 0 <= result.coverage <= 1
            or (
                result.coverage
                != (
                    0.0
                    if result.total_weight == 0
                    else result.attributed_weight / result.total_weight
                )
            )
        ):
            raise ValueError("inconsistent hotspot report totals")
        return result


def _finite_weight(value: object, name: str) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"invalid {name}")
    return float(value)


class _MachineResolver:
    def __init__(self, provenance: ProvenanceMap) -> None:
        ranges = sorted(
            (
                node.address_start,
                node.address_end,
                node.node_id,
            )
            for node in provenance.nodes
            if node.layer is ProvenanceLayer.MACHINE
        )
        previous_end = -1
        for start, end, _node_id in ranges:
            assert start is not None and end is not None
            if start < previous_end:
                raise ValueError("overlapping provenance machine ranges")
            previous_end = end
        self._ranges = tuple(ranges)
        self._starts = tuple(item[0] for item in ranges)

    def resolve(self, instruction_pointer: int) -> str | None:
        index = bisect.bisect_right(self._starts, instruction_pointer) - 1
        if index < 0:
            return None
        start, end, node_id = self._ranges[index]
        assert start is not None and end is not None
        return node_id if start <= instruction_pointer < end else None


def _reverse_adjacency(provenance: ProvenanceMap) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for edge in provenance.edges:
        if edge.to_node_id is not None:
            result.setdefault(edge.to_node_id, set()).add(edge.from_node_id)
    return result


def _upstream_keys(
    machine_id: str,
    *,
    layer: ProvenanceLayer,
    node_layers: dict[str, ProvenanceLayer],
    reverse: dict[str, set[str]],
) -> tuple[str, ...]:
    if layer is ProvenanceLayer.MACHINE:
        return (machine_id,)
    result: set[str] = set()
    seen = {machine_id}
    pending = list(reverse.get(machine_id, ()))
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        if node_layers[node_id] is layer:
            result.add(node_id)
        pending.extend(reverse.get(node_id, ()))
    return tuple(sorted(result))


def _entry_class(exact_weight: float, shared_weight: float) -> AttributionClass:
    if exact_weight and shared_weight:
        return AttributionClass.MIXED
    if shared_weight:
        return AttributionClass.SHARED
    return AttributionClass.EXACT


def project_hotspots(
    profile: NormalizedPerfProfile,
    provenance: ProvenanceMap,
    group_by: HotspotGroupBy | str,
) -> HotspotReport:
    """Project perf periods to one requested provenance layer."""

    group = HotspotGroupBy(group_by)
    verify_provenance_map(provenance)
    resolver = _MachineResolver(provenance)
    node_by_id = {node.node_id: node for node in provenance.nodes}
    node_layers = {
        node.node_id: node.layer for node in provenance.nodes
    }
    reverse = _reverse_adjacency(provenance)
    upstream_cache: dict[tuple[str, ProvenanceLayer], tuple[str, ...]] = {}
    accumulators: dict[str, list[float | int]] = {}
    exact_weight = 0
    shared_weight = 0
    unattributed_weight = 0

    for sample in profile.samples:
        machine_id = resolver.resolve(sample.ip)
        if group is HotspotGroupBy.PHASE:
            keys = () if sample.runtime_phase is None else (sample.runtime_phase,)
        elif group is HotspotGroupBy.SYMBOL:
            digest = None
            if machine_id is not None:
                digest = dict(node_by_id[machine_id].attributes).get(
                    "symbol_sha256"
                )
                if digest is not None:
                    digest = _text(
                        digest,
                        _SHA256,
                        "machine symbol hash",
                    )
            if digest is None:
                digest = sample.symbol_sha256
            keys = () if digest is None else (digest,)
        elif machine_id is None:
            keys = ()
        else:
            layer = _GROUP_LAYER[group]
            cache_key = (machine_id, layer)
            keys = upstream_cache.get(cache_key, ())
            if cache_key not in upstream_cache:
                keys = _upstream_keys(
                    machine_id,
                    layer=layer,
                    node_layers=node_layers,
                    reverse=reverse,
                )
                upstream_cache[cache_key] = keys

        if not keys:
            unattributed_weight += sample.period
            continue
        is_shared = len(keys) > 1
        share = sample.period / len(keys)
        if is_shared:
            shared_weight += sample.period
        else:
            exact_weight += sample.period
        for key in keys:
            values = accumulators.setdefault(key, [0.0, 0.0, 0.0, 0])
            values[0] += share
            values[1 if not is_shared else 2] += share
            values[3] += 1

    total_weight = sum(sample.period for sample in profile.samples)
    attributed_weight = exact_weight + shared_weight
    entries = tuple(
        sorted(
            (
                HotspotEntry(
                    key,
                    float(values[0]),
                    float(values[1]),
                    float(values[2]),
                    int(values[3]),
                    _entry_class(float(values[1]), float(values[2])),
                )
                for key, values in accumulators.items()
            ),
            key=lambda entry: (-entry.weight, entry.key),
        )
    )
    return HotspotReport(
        HOTSPOT_REPORT_VERSION,
        profile.run_id,
        profile.event,
        group,
        total_weight,
        attributed_weight,
        exact_weight,
        shared_weight,
        unattributed_weight,
        0.0 if total_weight == 0 else attributed_weight / total_weight,
        profile.lost_samples,
        entries,
    )


def diff_hotspot_reports(
    baseline: HotspotReport,
    candidate: HotspotReport,
) -> dict[str, object]:
    if (
        baseline.group_by is not candidate.group_by
        or baseline.event != candidate.event
    ):
        raise ValueError("hotspot reports are not comparable")
    baseline_entries = {entry.key: entry for entry in baseline.entries}
    candidate_entries = {entry.key: entry for entry in candidate.entries}
    entries = []
    for key in set(baseline_entries) | set(candidate_entries):
        before = baseline_entries.get(key)
        after = candidate_entries.get(key)
        before_weight = 0.0 if before is None else before.weight
        after_weight = 0.0 if after is None else after.weight
        entries.append(
            {
                "baseline_weight": before_weight,
                "candidate_weight": after_weight,
                "key": key,
                "weight_delta": after_weight - before_weight,
            }
        )
    entries.sort(key=lambda entry: (-abs(entry["weight_delta"]), entry["key"]))
    return {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "coverage_delta": candidate.coverage - baseline.coverage,
        "entries": entries,
        "event": baseline.event,
        "group_by": baseline.group_by.value,
        "schema_version": 1,
        "total_weight_delta": (
            candidate.total_weight - baseline.total_weight
        ),
    }
