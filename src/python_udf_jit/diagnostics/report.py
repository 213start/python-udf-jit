from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from python_udf_jit.runtime.variant import WorkerProcessKey


@dataclass(frozen=True)
class RuntimeEvent:
    """Value-free U5 evidence joined by run, process generation, and Variant Key."""

    stage: str
    decision: str
    reason_code: str
    run_id: str
    process: WorkerProcessKey
    variant_key: str = ""
    artifact_hash: str = ""
    code_hash: str = ""
    partition_id: str = ""
    task_attempt: str = ""
    execution_mode: str = ""
    timestamp_ns: int = field(default_factory=time.time_ns)


class RuntimeEventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> bool: ...


class InMemoryRuntimeReport:
    def __init__(self, max_events: int = 4096) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def emit(self, event: RuntimeEvent) -> bool:
        if not isinstance(event, RuntimeEvent):
            return False
        try:
            with self._lock:
                self._events.append(event)
            return True
        except Exception:
            return False

    def snapshot(self) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            return tuple(self._events)


DEFAULT_RUNTIME_REPORT = InMemoryRuntimeReport()


_PROVENANCE_ARTIFACT = "provenance/map.json"
_PERF_ARTIFACT = "perf/samples.json"


def _load_provenance(path: str | Path):
    # These imports stay behind the explicit query call.  Normal runtime
    # reporting must not load provenance or hotspot machinery.
    from python_udf_jit.diagnostics.bundle import (
        read_json_artifact,
        validate_bundle,
    )
    from python_udf_jit.diagnostics.provenance import ProvenanceMap

    bundle = validate_bundle(path)
    provenance = ProvenanceMap.from_document(
        read_json_artifact(bundle, _PROVENANCE_ARTIFACT)
    )
    return bundle, provenance


def _load_query_inputs(path: str | Path):
    from python_udf_jit.diagnostics.bundle import read_json_artifact
    from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile

    bundle, provenance = _load_provenance(path)
    profile = NormalizedPerfProfile.from_document(
        read_json_artifact(bundle, _PERF_ARTIFACT)
    )
    return bundle, provenance, profile


def _query_status(*bundles) -> str:
    from python_udf_jit.diagnostics.bundle import BundleStatus

    return (
        "incomplete"
        if any(bundle.status is BundleStatus.INCOMPLETE for bundle in bundles)
        else "valid"
    )


def validate_diagnostic_bundle(path: str | Path) -> dict[str, object]:
    """Validate bundle storage and any machine-to-source query inputs."""

    from python_udf_jit.diagnostics.bundle import (
        read_json_artifact,
        validate_bundle,
    )
    from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile
    from python_udf_jit.diagnostics.provenance import ProvenanceMap

    bundle = validate_bundle(path)
    evidence_refs = [artifact.path for artifact in bundle.artifacts]
    if _PROVENANCE_ARTIFACT in evidence_refs:
        ProvenanceMap.from_document(
            read_json_artifact(bundle, _PROVENANCE_ARTIFACT)
        )
    if _PERF_ARTIFACT in evidence_refs:
        NormalizedPerfProfile.from_document(
            read_json_artifact(bundle, _PERF_ARTIFACT)
        )
    return {
        "artifact_count": len(bundle.artifacts),
        "bundle_status": bundle.status.value,
        "evidence_refs": evidence_refs,
        "executed_content": False,
        "run_id": bundle.manifest["run_id"],
        "schema_version": 1,
        "status": _query_status(bundle),
    }


def trace_diagnostic_bundle(
    path: str | Path,
    node_id: str,
    *,
    direction: str = "both",
) -> dict[str, object]:
    """Trace a stable provenance ID without loading executable artifacts."""

    if direction not in ("upstream", "downstream", "both"):
        raise ValueError("invalid trace direction")
    bundle, provenance = _load_provenance(path)
    node_by_id = {node.node_id: node for node in provenance.nodes}
    if node_id not in node_by_id:
        raise KeyError(node_id)
    results: dict[str, object] = {
        "evidence_refs": [_PROVENANCE_ARTIFACT],
        "executed_content": False,
        "node": node_by_id[node_id].to_document(),
        "run_id": bundle.manifest["run_id"],
        "schema_version": 1,
        "status": _query_status(bundle),
    }
    if direction in ("upstream", "both"):
        results["upstream"] = [
            node.to_document()
            for node in provenance.trace_upstream(node_id)
        ]
    if direction in ("downstream", "both"):
        results["downstream"] = [
            node.to_document()
            for node in provenance.trace_downstream(node_id)
        ]
    return results


def hotspots_diagnostic_bundle(
    path: str | Path,
    *,
    group_by: str,
) -> dict[str, object]:
    """Project normalized perf periods onto one provenance layer."""

    from python_udf_jit.diagnostics.hotspots import project_hotspots

    bundle, provenance, profile = _load_query_inputs(path)
    report = project_hotspots(profile, provenance, group_by)
    return {
        "coverage": report.coverage,
        "evidence_refs": [_PERF_ARTIFACT, _PROVENANCE_ARTIFACT],
        "executed_content": False,
        "results": report.to_document(),
        "run_id": bundle.manifest["run_id"],
        "schema_version": 1,
        "status": _query_status(bundle),
    }


def _artifact_changes(baseline, candidate) -> list[dict[str, object]]:
    baseline_by_path = {
        artifact.path: artifact for artifact in baseline.artifacts
    }
    candidate_by_path = {
        artifact.path: artifact for artifact in candidate.artifacts
    }
    changes: list[dict[str, object]] = []
    for path in sorted(set(baseline_by_path) | set(candidate_by_path)):
        before = baseline_by_path.get(path)
        after = candidate_by_path.get(path)
        if before is not None and after is not None:
            if before.sha256 == after.sha256:
                continue
            change = "modified"
        else:
            change = "added" if before is None else "removed"
        changes.append(
            {
                "baseline_sha256": None if before is None else before.sha256,
                "candidate_sha256": None if after is None else after.sha256,
                "change": change,
                "path": path,
            }
        )
    return changes


def diff_diagnostic_bundles(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    group_by: str = "source",
) -> dict[str, object]:
    """Compare bundle artifact hashes and provenance-projected hotspots."""

    from python_udf_jit.diagnostics.hotspots import (
        diff_hotspot_reports,
        project_hotspots,
    )

    baseline, baseline_provenance, baseline_profile = _load_query_inputs(
        baseline_path
    )
    candidate, candidate_provenance, candidate_profile = _load_query_inputs(
        candidate_path
    )
    baseline_hotspots = project_hotspots(
        baseline_profile,
        baseline_provenance,
        group_by,
    )
    candidate_hotspots = project_hotspots(
        candidate_profile,
        candidate_provenance,
        group_by,
    )
    hotspot_difference = diff_hotspot_reports(
        baseline_hotspots,
        candidate_hotspots,
    )
    return {
        "coverage": {
            "baseline": baseline_hotspots.coverage,
            "candidate": candidate_hotspots.coverage,
        },
        "evidence_refs": [_PERF_ARTIFACT, _PROVENANCE_ARTIFACT],
        "executed_content": False,
        "results": {
            "artifacts": _artifact_changes(baseline, candidate),
            "hotspots": hotspot_difference,
        },
        "schema_version": 1,
        "status": _query_status(baseline, candidate),
    }
