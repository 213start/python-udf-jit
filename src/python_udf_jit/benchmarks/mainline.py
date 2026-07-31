"""Versioned profiling contract for the scalar production mainline."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_LEGACY_PROFILE_VERSION = 1
_PROFILE_VERSION = 2


class ProfileError(ValueError):
    """A mainline performance profile is malformed or non-reproducible."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{field}_invalid")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ProfileError("correctness_value_not_canonical_json") from error


def canonical_correctness_sha256(value: object) -> str:
    """Hash ordered/canonical correctness evidence, never object ``repr``."""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class EnvironmentFingerprint:
    python_version: str
    cinderx_commit: str
    cinderx_soabi: str
    daft_version: str
    ray_version: str
    lance_version: str
    pyarrow_version: str
    machine: str
    cpu_model: str
    support_matrix_sha256: str
    policy_version: str

    def __post_init__(self) -> None:
        for field in (
            "python_version",
            "cinderx_soabi",
            "daft_version",
            "ray_version",
            "lance_version",
            "pyarrow_version",
            "machine",
            "cpu_model",
            "policy_version",
        ):
            _text(getattr(self, field), field)
        if _GIT_COMMIT.fullmatch(self.cinderx_commit) is None:
            raise ProfileError("cinderx_commit_invalid")
        if _SHA256.fullmatch(self.support_matrix_sha256) is None:
            raise ProfileError("support_matrix_sha256_invalid")

    def _identity_document(self) -> dict[str, str]:
        return {
            "cinderx_commit": self.cinderx_commit,
            "cinderx_soabi": self.cinderx_soabi,
            "cpu_model": self.cpu_model,
            "daft_version": self.daft_version,
            "lance_version": self.lance_version,
            "machine": self.machine,
            "policy_version": self.policy_version,
            "pyarrow_version": self.pyarrow_version,
            "python_version": self.python_version,
            "ray_version": self.ray_version,
            "support_matrix_sha256": self.support_matrix_sha256,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _canonical_bytes(self._identity_document())
        ).hexdigest()

    def to_document(self) -> dict[str, str]:
        return {
            **self._identity_document(),
            "fingerprint_sha256": self.sha256,
        }


class MainlineProfile:
    """Deterministic phase aggregation for a non-gating directional A/B."""

    def __init__(
        self,
        *,
        run_id: str,
        environment: EnvironmentFingerprint,
        correctness_sha256: str,
        functional_status: str = "pass",
        diagnostic_profile: str = "off",
    ) -> None:
        self._run_id = _text(run_id, "run_id")
        if not isinstance(environment, EnvironmentFingerprint):
            raise ProfileError("environment_invalid")
        if _SHA256.fullmatch(correctness_sha256) is None:
            raise ProfileError("correctness_sha256_invalid")
        if functional_status not in {"pass", "fail"}:
            raise ProfileError("functional_status_invalid")
        if diagnostic_profile != "off":
            raise ProfileError("diagnostics_must_be_off")
        self._environment = environment
        self._correctness_sha256 = correctness_sha256
        self._functional_status = functional_status
        self._samples: dict[str, list[int]] = {}
        self._performance: dict[str, object] = {
            "mode": "directional",
            "status": "not_measured",
            "baseline_ns": None,
            "candidate_ns": None,
            "speedup": None,
            "reference_target_speedup": 1.15,
            "conclusion_scope": "directional_only",
            "blocks_functional_completion": False,
        }

    def record_phase(self, phase: str, duration_ns: int) -> None:
        if not isinstance(phase, str) or _PHASE.fullmatch(phase) is None:
            raise ProfileError("phase_invalid")
        if type(duration_ns) is not int or duration_ns < 0:
            raise ProfileError("duration_ns_invalid")
        self._samples.setdefault(phase, []).append(duration_ns)

    @contextmanager
    def measure_phase(
        self,
        phase: str,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> Iterator[None]:
        """Measure one phase; an injected monotonic clock makes tests repeatable."""

        started = clock_ns()
        try:
            yield
        finally:
            finished = clock_ns()
            self.record_phase(phase, finished - started)

    def assess_performance(
        self,
        *,
        baseline_ns: int,
        candidate_ns: int,
        reference_target_speedup: float = 1.15,
    ) -> None:
        if (
            type(baseline_ns) is not int
            or baseline_ns <= 0
            or type(candidate_ns) is not int
            or candidate_ns <= 0
            or isinstance(reference_target_speedup, bool)
            or not isinstance(reference_target_speedup, (int, float))
            or not math.isfinite(float(reference_target_speedup))
            or reference_target_speedup <= 0
        ):
            raise ProfileError("performance_assessment_invalid")
        speedup = baseline_ns / candidate_ns
        self._performance = {
            "mode": "directional",
            "status": "directional_recorded",
            "baseline_ns": baseline_ns,
            "candidate_ns": candidate_ns,
            "speedup": speedup,
            "reference_target_speedup": float(reference_target_speedup),
            "conclusion_scope": "directional_only",
            # KTD14: performance is attribution in U1, not functional completion.
            "blocks_functional_completion": False,
        }

    def _phase_documents(self) -> list[dict[str, object]]:
        documents = []
        for phase, samples in sorted(self._samples.items()):
            documents.append(
                {
                    "phase": phase,
                    "samples_ns": list(samples),
                    "sample_count": len(samples),
                    "total_ns": sum(samples),
                    "median_ns": statistics.median(samples),
                }
            )
        return documents

    def to_document(self) -> dict[str, object]:
        phases = self._phase_documents()
        hotspots = [
            {
                "phase": phase["phase"],
                "total_ns": phase["total_ns"],
                "sample_count": phase["sample_count"],
            }
            for phase in sorted(
                phases,
                key=lambda item: (-int(item["total_ns"]), str(item["phase"])),
            )
        ]
        return {
            "schema_version": _PROFILE_VERSION,
            "profile": "mainline-production",
            "run_id": self._run_id,
            "environment": self._environment.to_document(),
            "correctness_sha256": self._correctness_sha256,
            "functional_status": self._functional_status,
            "diagnostics": "off",
            "phase_timings": phases,
            "hotspots": hotspots,
            "performance": dict(self._performance),
        }


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileError(f"{field}_invalid")
    return value


def validate_profile_document(document: object) -> None:
    """Validate the closed report shape without a runtime JSON-schema dependency."""

    root = _mapping(document, "profile")
    legacy_fields = {
        "schema_version",
        "profile",
        "run_id",
        "environment",
        "correctness_sha256",
        "functional_status",
        "phase_timings",
        "hotspots",
        "performance",
    }
    profile_version = root.get("schema_version")
    if profile_version == _LEGACY_PROFILE_VERSION:
        expected = legacy_fields
    elif profile_version == _PROFILE_VERSION:
        expected = legacy_fields | {"diagnostics"}
    else:
        raise ProfileError("profile_identity_invalid")
    if "diagnostics" in root and root["diagnostics"] != "off":
        raise ProfileError("diagnostics_must_be_off")
    if set(root) != expected:
        raise ProfileError("profile_fields_invalid")
    if root["profile"] != "mainline-production":
        raise ProfileError("profile_identity_invalid")
    _text(root["run_id"], "run_id")
    if (
        not isinstance(root["correctness_sha256"], str)
        or _SHA256.fullmatch(root["correctness_sha256"]) is None
    ):
        raise ProfileError("correctness_sha256_invalid")
    if root["functional_status"] not in {"pass", "fail"}:
        raise ProfileError("functional_status_invalid")

    environment = _mapping(root["environment"], "environment")
    environment_fields = {
        "python_version",
        "cinderx_commit",
        "cinderx_soabi",
        "daft_version",
        "ray_version",
        "lance_version",
        "pyarrow_version",
        "machine",
        "cpu_model",
        "support_matrix_sha256",
        "policy_version",
        "fingerprint_sha256",
    }
    if set(environment) != environment_fields:
        raise ProfileError("environment_fields_invalid")
    for field in environment_fields - {
        "cinderx_commit",
        "support_matrix_sha256",
        "fingerprint_sha256",
    }:
        _text(environment[field], f"environment_{field}")
    if (
        not isinstance(environment["cinderx_commit"], str)
        or _GIT_COMMIT.fullmatch(environment["cinderx_commit"]) is None
    ):
        raise ProfileError("environment_cinderx_commit_invalid")
    if (
        not isinstance(environment["support_matrix_sha256"], str)
        or _SHA256.fullmatch(environment["support_matrix_sha256"]) is None
    ):
        raise ProfileError("environment_support_matrix_sha256_invalid")
    identity = dict(environment)
    fingerprint = identity.pop("fingerprint_sha256")
    if (
        not isinstance(fingerprint, str)
        or fingerprint
        != hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    ):
        raise ProfileError("environment_fingerprint_invalid")

    phases = root["phase_timings"]
    if not isinstance(phases, list):
        raise ProfileError("phase_timings_invalid")
    seen: set[str] = set()
    expected_hotspots: list[dict[str, object]] = []
    for raw_phase in phases:
        phase = _mapping(raw_phase, "phase_timing")
        if set(phase) != {
            "phase",
            "samples_ns",
            "sample_count",
            "total_ns",
            "median_ns",
        }:
            raise ProfileError("phase_timing_fields_invalid")
        name = phase["phase"]
        samples = phase["samples_ns"]
        if (
            not isinstance(name, str)
            or _PHASE.fullmatch(name) is None
            or name in seen
            or not isinstance(samples, list)
            or not samples
            or any(type(value) is not int or value < 0 for value in samples)
            or phase["sample_count"] != len(samples)
            or phase["total_ns"] != sum(samples)
            or phase["median_ns"] != statistics.median(samples)
        ):
            raise ProfileError("phase_timing_invalid")
        seen.add(name)
        expected_hotspots.append(
            {
                "phase": name,
                "total_ns": sum(samples),
                "sample_count": len(samples),
            }
        )
    expected_hotspots.sort(
        key=lambda item: (-int(item["total_ns"]), str(item["phase"]))
    )
    if root["hotspots"] != expected_hotspots:
        raise ProfileError("hotspot_order_invalid")

    performance = _mapping(root["performance"], "performance")
    if set(performance) != {
        "mode",
        "status",
        "baseline_ns",
        "candidate_ns",
        "speedup",
        "reference_target_speedup",
        "conclusion_scope",
        "blocks_functional_completion",
    }:
        raise ProfileError("performance_fields_invalid")
    mode = performance["mode"]
    status = performance["status"]
    if mode != "directional" or status not in {
        "not_measured",
        "directional_recorded",
    }:
        raise ProfileError("performance_status_invalid")
    if performance["conclusion_scope"] != "directional_only":
        raise ProfileError("performance_conclusion_scope_invalid")
    if performance["blocks_functional_completion"] is not False:
        raise ProfileError("performance_must_not_block_functionality")
    target = performance["reference_target_speedup"]
    if (
        isinstance(target, bool)
        or not isinstance(target, (int, float))
        or not math.isfinite(float(target))
        or target <= 0
    ):
        raise ProfileError("performance_target_invalid")
    baseline = performance["baseline_ns"]
    candidate = performance["candidate_ns"]
    speedup = performance["speedup"]
    if status == "not_measured":
        if (
            baseline is not None
            or candidate is not None
            or speedup is not None
        ):
            raise ProfileError("unmeasured_performance_inconsistent")
        return
    if (
        type(baseline) is not int
        or baseline <= 0
        or type(candidate) is not int
        or candidate <= 0
        or isinstance(speedup, bool)
        or not isinstance(speedup, (int, float))
        or not math.isfinite(float(speedup))
        or speedup <= 0
        or speedup != baseline / candidate
    ):
        raise ProfileError("measured_performance_invalid")
    if status != "directional_recorded":
        raise ProfileError("directional_performance_inconsistent")
