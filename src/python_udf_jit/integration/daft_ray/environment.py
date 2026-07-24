from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping


class ContractViolation(ValueError):
    """Raised when evidence cannot satisfy the locked piercing contract."""


class DockerPreflightStatus(StrEnum):
    READY = "ready"
    NEEDS_BOOTSTRAP = "needs_bootstrap"


@dataclass(frozen=True)
class DockerPreflightResult:
    status: DockerPreflightStatus
    reason: str
    local_ray_fallback_allowed: bool = False


@dataclass(frozen=True)
class EnvironmentContract:
    profile: str
    plugin_mode: str
    locked_versions: Mapping[str, str]
    non_blocking_versions: Mapping[str, str]
    required_fingerprints: tuple[str, ...]
    ray_daft_mismatch_policy: str


@dataclass(frozen=True)
class ValidatedRuntimeFingerprints:
    blocking_fingerprint: tuple[tuple[str, str], ...]
    roles: tuple[str, ...]


def load_environment_contract(path: str | Path) -> EnvironmentContract:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        policy = document["compatibility_policy"]
        contract = EnvironmentContract(
            profile=document["profile"],
            plugin_mode=document["plugin_mode"],
            locked_versions=dict(document["locked_versions"]),
            non_blocking_versions=dict(document["non_blocking_versions"]),
            required_fingerprints=tuple(document["required_fingerprints"]),
            ray_daft_mismatch_policy=policy["ray_daft_mismatch"],
        )
    except (KeyError, TypeError) as error:
        raise ContractViolation(f"invalid environment contract: {error}") from error
    if policy.get("local_ray_fallback_allowed") is not False:
        raise ContractViolation("local Ray fallback must be disabled")
    return contract


def preflight_docker(executable: str = "docker") -> DockerPreflightResult:
    resolved = shutil.which(executable)
    if resolved is None:
        return DockerPreflightResult(
            DockerPreflightStatus.NEEDS_BOOTSTRAP,
            f"Docker executable {executable!r} not found",
        )
    try:
        completed = subprocess.run(
            [resolved, "info", "--format", "{{json .ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return DockerPreflightResult(
            DockerPreflightStatus.NEEDS_BOOTSTRAP,
            f"Docker backend unavailable: {error}",
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "docker info failed"
        return DockerPreflightResult(
            DockerPreflightStatus.NEEDS_BOOTSTRAP,
            f"Docker backend unavailable: {detail}",
        )
    return DockerPreflightResult(DockerPreflightStatus.READY, "Docker backend ready")


def validate_runtime_fingerprints(
    contract: EnvironmentContract,
    reports: Iterable[Mapping[str, Any]],
) -> ValidatedRuntimeFingerprints:
    expected_roles = {"ray-head-driver", "ray-worker-1", "ray-worker-2"}
    reports_by_role: dict[str, Mapping[str, Any]] = {}
    for report in reports:
        role = str(report.get("role", ""))
        if not role or role in reports_by_role:
            raise ContractViolation("runtime reports require unique non-empty roles")
        reports_by_role[role] = report
    if set(reports_by_role) != expected_roles:
        raise ContractViolation("runtime reports must contain exactly the three fixed roles")

    version_fields = {
        "python": "python_version",
        "daft": "daft_version",
        "ray": "ray_version",
        "pyarrow": "pyarrow_version",
    }
    fingerprints: list[tuple[tuple[str, str], ...]] = []
    for role in sorted(expected_roles):
        report = reports_by_role[role]
        missing = [key for key in contract.required_fingerprints if key not in report]
        if missing:
            raise ContractViolation(f"{role} missing fingerprints: {', '.join(missing)}")
        for version_name, report_field in version_fields.items():
            if str(report[report_field]) != contract.locked_versions[version_name]:
                raise ContractViolation(f"fingerprint drift at {role}: {report_field}")
        fingerprints.append(
            tuple(sorted((key, str(report[key])) for key in contract.required_fingerprints))
        )
    if any(value != fingerprints[0] for value in fingerprints[1:]):
        raise ContractViolation("fingerprint drift across cluster roles")
    return ValidatedRuntimeFingerprints(fingerprints[0], tuple(sorted(expected_roles)))
