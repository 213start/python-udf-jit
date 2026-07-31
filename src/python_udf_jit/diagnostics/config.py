"""Frozen startup configuration for isolated diagnostics."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping


_SCHEMA_VERSION = 1
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_MAX_MAX_BYTES = 1024 * 1024 * 1024
_SELECTOR = re.compile(r"^[\x21-\x7e]{1,512}$")


class DiagnosticProfile(StrEnum):
    OFF = "off"
    SUMMARY = "summary"
    FULL = "full"


class DiagnosticSourcePolicy(StrEnum):
    RANGES = "ranges"
    TEXT = "text"


class DiagnosticPerfMode(StrEnum):
    OFF = "off"
    RECORD = "record"


class DiagnosticConfigurationIssue(StrEnum):
    PROFILE_INVALID = "profile_invalid"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_NOT_ABSOLUTE = "output_not_absolute"
    OUTPUT_UNSAFE_ROOT = "output_unsafe_root"
    OUTPUT_SYMLINK = "output_symlink"
    FILTER_INVALID = "filter_invalid"
    SOURCE_INVALID = "source_invalid"
    SOURCE_UNSUPPORTED = "source_unsupported"
    PERF_INVALID = "perf_invalid"
    PERF_UNSUPPORTED = "perf_unsupported"
    SAMPLE_RATE_INVALID = "sample_rate_invalid"
    MAX_BYTES_INVALID = "max_bytes_invalid"
    FULL_REQUIRES_DEDICATED_WORKER = "full_requires_dedicated_worker"


class DiagnosticConfigurationError(ValueError):
    """An explicit diagnostics request was unsafe or internally inconsistent."""

    reason_code = "diagnostics_configuration_invalid"

    def __init__(self, issue: DiagnosticConfigurationIssue) -> None:
        self.issue = issue
        super().__init__(f"{self.reason_code}:{issue.value}")


@dataclass(frozen=True)
class DiagnosticRuntimeContext:
    """Startup facts that cannot be inferred safely from environment strings."""

    dedicated_worker: bool = False
    workspace_root: Path | None = None
    home_root: Path | None = None
    forbidden_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class DiagnosticPolicySnapshot:
    """Immutable policy bound once at Driver or Worker startup."""

    profile: DiagnosticProfile
    output_root: Path | None
    selector: str
    source_policy: DiagnosticSourcePolicy
    perf_mode: DiagnosticPerfMode
    sample_rate: float
    max_bytes: int
    requires_dedicated_worker: bool
    sha256: str

    @property
    def enabled(self) -> bool:
        return self.profile is not DiagnosticProfile.OFF

    @property
    def document(self) -> dict[str, object]:
        return _policy_document(
            profile=self.profile,
            output_root=self.output_root,
            selector=self.selector,
            source_policy=self.source_policy,
            perf_mode=self.perf_mode,
            sample_rate=self.sample_rate,
            max_bytes=self.max_bytes,
            requires_dedicated_worker=self.requires_dedicated_worker,
        )


_OFF_DOCUMENT_BYTES = (
    b'{"max_bytes":0,"output_root":null,"perf_mode":"off","profile":"off",'
    b'"requires_dedicated_worker":false,"sample_rate":0.0,"schema_version":1,'
    b'"selector":"","source_policy":"ranges"}'
)
OFF_DIAGNOSTIC_POLICY = DiagnosticPolicySnapshot(
    profile=DiagnosticProfile.OFF,
    output_root=None,
    selector="",
    source_policy=DiagnosticSourcePolicy.RANGES,
    perf_mode=DiagnosticPerfMode.OFF,
    sample_rate=0.0,
    max_bytes=0,
    requires_dedicated_worker=False,
    sha256=hashlib.sha256(_OFF_DOCUMENT_BYTES).hexdigest(),
)


def _fail(issue: DiagnosticConfigurationIssue) -> None:
    raise DiagnosticConfigurationError(issue)


def _enum(
    enum_type: type[StrEnum],
    raw: object,
    issue: DiagnosticConfigurationIssue,
) -> StrEnum:
    if not isinstance(raw, str):
        _fail(issue)
    try:
        return enum_type(raw)
    except ValueError:
        _fail(issue)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _normalized_root(path: Path) -> Path:
    return path.resolve(strict=False)


def _safe_output_root(
    raw: object,
    context: DiagnosticRuntimeContext,
) -> Path:
    if not isinstance(raw, str) or not raw:
        _fail(DiagnosticConfigurationIssue.OUTPUT_MISSING)
    path = Path(raw)
    if not path.is_absolute():
        _fail(DiagnosticConfigurationIssue.OUTPUT_NOT_ABSOLUTE)
    if ".." in path.parts:
        _fail(DiagnosticConfigurationIssue.OUTPUT_UNSAFE_ROOT)
    if _has_symlink_component(path):
        _fail(DiagnosticConfigurationIssue.OUTPUT_SYMLINK)
    normalized = _normalized_root(path)
    unsafe_roots = {
        Path(os.path.abspath(os.sep)),
        _normalized_root(context.home_root or Path.home()),
        _normalized_root(context.workspace_root or Path.cwd()),
        *(_normalized_root(root) for root in context.forbidden_roots),
    }
    if normalized in unsafe_roots:
        _fail(DiagnosticConfigurationIssue.OUTPUT_UNSAFE_ROOT)
    return normalized


def _parse_selector(raw: object) -> str:
    if (
        not isinstance(raw, str)
        or _SELECTOR.fullmatch(raw) is None
        or "\\" in raw
    ):
        _fail(DiagnosticConfigurationIssue.FILTER_INVALID)
    return raw


def _parse_sample_rate(raw: object) -> float:
    if not isinstance(raw, str):
        _fail(DiagnosticConfigurationIssue.SAMPLE_RATE_INVALID)
    try:
        value = float(raw)
    except ValueError:
        _fail(DiagnosticConfigurationIssue.SAMPLE_RATE_INVALID)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        _fail(DiagnosticConfigurationIssue.SAMPLE_RATE_INVALID)
    return value


def _parse_max_bytes(raw: object) -> int:
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        _fail(DiagnosticConfigurationIssue.MAX_BYTES_INVALID)
    value = int(raw)
    if value <= 0 or value > _MAX_MAX_BYTES:
        _fail(DiagnosticConfigurationIssue.MAX_BYTES_INVALID)
    return value


def _policy_document(
    *,
    profile: DiagnosticProfile,
    output_root: Path | None,
    selector: str,
    source_policy: DiagnosticSourcePolicy,
    perf_mode: DiagnosticPerfMode,
    sample_rate: float,
    max_bytes: int,
    requires_dedicated_worker: bool,
) -> dict[str, object]:
    return {
        "max_bytes": max_bytes,
        "output_root": None if output_root is None else os.fspath(output_root),
        "perf_mode": perf_mode.value,
        "profile": profile.value,
        "requires_dedicated_worker": requires_dedicated_worker,
        "sample_rate": sample_rate,
        "schema_version": _SCHEMA_VERSION,
        "selector": selector,
        "source_policy": source_policy.value,
    }


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def resolve_diagnostic_policy(
    environment: Mapping[str, str],
    runtime_context: DiagnosticRuntimeContext | None = None,
) -> DiagnosticPolicySnapshot:
    """Parse an environment once; explicit diagnostics always fail closed."""

    context = runtime_context or DiagnosticRuntimeContext()
    raw_profile = environment.get("UDFJIT_DIAGNOSTICS", "off")
    profile = _enum(
        DiagnosticProfile,
        raw_profile,
        DiagnosticConfigurationIssue.PROFILE_INVALID,
    )
    assert isinstance(profile, DiagnosticProfile)
    if profile is DiagnosticProfile.OFF:
        return OFF_DIAGNOSTIC_POLICY

    output_root = _safe_output_root(
        environment.get("UDFJIT_DIAGNOSTIC_DIR"),
        context,
    )
    selector = _parse_selector(
        environment.get("UDFJIT_DIAGNOSTIC_FILTER")
    )
    source_policy = _enum(
        DiagnosticSourcePolicy,
        environment.get("UDFJIT_DIAGNOSTIC_SOURCE", "ranges"),
        DiagnosticConfigurationIssue.SOURCE_INVALID,
    )
    perf_mode = _enum(
        DiagnosticPerfMode,
        environment.get("UDFJIT_DIAGNOSTIC_PERF", "off"),
        DiagnosticConfigurationIssue.PERF_INVALID,
    )
    assert isinstance(source_policy, DiagnosticSourcePolicy)
    assert isinstance(perf_mode, DiagnosticPerfMode)
    sample_rate = _parse_sample_rate(
        environment.get("UDFJIT_DIAGNOSTIC_SAMPLE_RATE", "1")
    )
    max_bytes = _parse_max_bytes(
        environment.get(
            "UDFJIT_DIAGNOSTIC_MAX_BYTES",
            str(_DEFAULT_MAX_BYTES),
        )
    )

    if (
        profile is DiagnosticProfile.SUMMARY
        and perf_mode is not DiagnosticPerfMode.OFF
    ):
        _fail(DiagnosticConfigurationIssue.PERF_UNSUPPORTED)
    if (
        profile is DiagnosticProfile.SUMMARY
        and source_policy is DiagnosticSourcePolicy.TEXT
    ):
        _fail(DiagnosticConfigurationIssue.SOURCE_UNSUPPORTED)
    if profile is DiagnosticProfile.FULL and not context.dedicated_worker:
        _fail(
            DiagnosticConfigurationIssue.FULL_REQUIRES_DEDICATED_WORKER
        )

    requires_dedicated_worker = profile is DiagnosticProfile.FULL
    document = _policy_document(
        profile=profile,
        output_root=output_root,
        selector=selector,
        source_policy=source_policy,
        perf_mode=perf_mode,
        sample_rate=sample_rate,
        max_bytes=max_bytes,
        requires_dedicated_worker=requires_dedicated_worker,
    )
    digest = hashlib.sha256(_canonical_json(document)).hexdigest()
    return DiagnosticPolicySnapshot(
        profile=profile,
        output_root=output_root,
        selector=selector,
        source_policy=source_policy,
        perf_mode=perf_mode,
        sample_rate=sample_rate,
        max_bytes=max_bytes,
        requires_dedicated_worker=requires_dedicated_worker,
        sha256=digest,
    )
