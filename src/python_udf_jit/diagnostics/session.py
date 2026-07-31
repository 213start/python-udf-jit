"""Diagnostic sessions selected once outside execution hot paths."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

from python_udf_jit.diagnostics.bundle import (
    ArtifactRef,
    BundleRef,
    BundleStatus,
    BundleWriter,
)
from python_udf_jit.diagnostics.config import DiagnosticPolicySnapshot

if TYPE_CHECKING:
    from types import TracebackType


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_MAX_RECORDS = 4096


@dataclass(frozen=True)
class DiagnosticStageProfile:
    stage: str
    identity: str
    duration_ns: int
    failed: bool = False

    @property
    def document(self) -> dict[str, object]:
        return {
            "duration_ns": self.duration_ns,
            "failed": self.failed,
            "identity": self.identity,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class DiagnosticMetric:
    name: str
    value: int | float
    identity: str


class _NoopSpan:
    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        return False


_NOOP_SPAN = _NoopSpan()


class NoopDiagnosticSession:
    """Allocation-free behavior after the process binds diagnostics=off."""

    __slots__ = ()

    def span(self, _stage: str, _identity: str = "") -> _NoopSpan:
        return _NOOP_SPAN

    def record_metric(
        self,
        _name: str,
        _value: int | float,
        _identity: str = "",
    ) -> bool:
        return False

    def record_nodes(self, _nodes: Iterable[Mapping[str, object]]) -> bool:
        return False

    def record_edges(self, _edges: Iterable[Mapping[str, object]]) -> bool:
        return False

    def record_artifact(
        self,
        _path: str,
        _media_type: str,
        _payload: bytes | str,
        _metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef | None:
        return None

    def stage_profiles(self) -> tuple[DiagnosticStageProfile, ...]:
        return ()

    def finalize(self, _status: BundleStatus) -> BundleRef | None:
        return None


NOOP_DIAGNOSTIC_SESSION = NoopDiagnosticSession()


class _DiagnosticSpan:
    __slots__ = ("_session", "_stage", "_identity", "_started_ns")

    def __init__(
        self,
        session: DiagnosticSession,
        stage: str,
        identity: str,
    ) -> None:
        self._session = session
        self._stage = stage
        self._identity = identity
        self._started_ns: int | None = None

    def __enter__(self) -> _DiagnosticSpan:
        try:
            self._started_ns = self._session._clock_ns()
        except Exception:
            self._session._record_failure()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if self._started_ns is None:
            return False
        try:
            ended_ns = self._session._clock_ns()
            duration_ns = max(0, ended_ns - self._started_ns)
            self._session._record_profile(
                DiagnosticStageProfile(
                    stage=self._stage,
                    identity=self._identity,
                    duration_ns=duration_ns,
                    failed=exception_type is not None,
                )
            )
        except Exception:
            self._session._record_failure()
        return False


class DiagnosticSession:
    """Low-frequency recorder used only by summary/full diagnostic runs."""

    def __init__(
        self,
        policy: DiagnosticPolicySnapshot,
        *,
        clock_ns,
        bundle_writer: BundleWriter | None = None,
    ) -> None:
        if not policy.enabled:
            raise ValueError("diagnostic_session_requires_enabled_policy")
        if not callable(clock_ns):
            raise ValueError("diagnostic_clock_invalid")
        self.policy = policy
        self._clock_ns = clock_ns
        self._bundle_writer = bundle_writer
        self._profiles: list[DiagnosticStageProfile] = []
        self._metrics: list[DiagnosticMetric] = []
        self._nodes: list[dict[str, object]] = []
        self._edges: list[dict[str, object]] = []
        self._dropped = 0
        self._failures = 0
        self._lock = threading.Lock()
        self._finalized = False

    @staticmethod
    def _safe_name(value: str, *, allow_empty: bool = False) -> str:
        if allow_empty and value == "":
            return value
        if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
            raise ValueError("diagnostic_identity_invalid")
        return value

    def span(self, stage: str, identity: str = "") -> _DiagnosticSpan:
        return _DiagnosticSpan(
            self,
            self._safe_name(stage),
            self._safe_name(identity, allow_empty=True),
        )

    def _record_profile(self, profile: DiagnosticStageProfile) -> None:
        with self._lock:
            if len(self._profiles) >= _MAX_RECORDS:
                self._dropped += 1
                return
            self._profiles.append(profile)

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1

    def record_metric(
        self,
        name: str,
        value: int | float,
        identity: str = "",
    ) -> bool:
        try:
            safe_name = self._safe_name(name)
            safe_identity = self._safe_name(identity, allow_empty=True)
            if (
                type(value) not in (int, float)
                or value < 0
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                return False
            with self._lock:
                if len(self._metrics) >= _MAX_RECORDS:
                    self._dropped += 1
                    return False
                self._metrics.append(
                    DiagnosticMetric(safe_name, value, safe_identity)
                )
            return True
        except Exception:
            self._record_failure()
            return False

    def _record_documents(
        self,
        target: list[dict[str, object]],
        documents: Iterable[Mapping[str, object]],
    ) -> bool:
        try:
            copied = [dict(document) for document in documents]
            json.dumps(
                copied,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            with self._lock:
                if len(target) + len(copied) > _MAX_RECORDS:
                    self._dropped += len(copied)
                    return False
                target.extend(copied)
            return True
        except Exception:
            self._record_failure()
            return False

    def record_nodes(
        self,
        nodes: Iterable[Mapping[str, object]],
    ) -> bool:
        return self._record_documents(self._nodes, nodes)

    def record_edges(
        self,
        edges: Iterable[Mapping[str, object]],
    ) -> bool:
        return self._record_documents(self._edges, edges)

    def record_artifact(
        self,
        path: str,
        media_type: str,
        payload: bytes | str,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef | None:
        if self._bundle_writer is None:
            return None
        try:
            return self._bundle_writer.add(
                path,
                media_type,
                payload,
                metadata,
            )
        except Exception:
            self._record_failure()
            return None

    def stage_profiles(self) -> tuple[DiagnosticStageProfile, ...]:
        with self._lock:
            return tuple(self._profiles)

    def _stage_payload(self) -> bytes:
        with self._lock:
            document = {
                "dropped": self._dropped,
                "failures": self._failures,
                "metrics": [
                    {
                        "identity": metric.identity,
                        "name": metric.name,
                        "value": metric.value,
                    }
                    for metric in self._metrics
                ],
                "profiles": [profile.document for profile in self._profiles],
            }
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")

    def finalize(self, status: BundleStatus) -> BundleRef | None:
        if self._finalized:
            return None
        self._finalized = True
        if self._bundle_writer is None:
            return None
        try:
            with self._lock:
                degraded = (
                    self._dropped > 0
                    or self._failures > 0
                    or any(profile.failed for profile in self._profiles)
                )
            if status is BundleStatus.COMPLETE and degraded:
                status = BundleStatus.PARTIAL
            self._bundle_writer.add(
                "reports/stages.json",
                "application/json",
                self._stage_payload(),
                {"layer": "reports"},
            )
            if status is BundleStatus.INCOMPLETE:
                return self._bundle_writer.abort("diagnostic_incomplete")
            return self._bundle_writer.complete(status)
        except Exception:
            return None


def open_diagnostic_session(
    policy: DiagnosticPolicySnapshot,
    *,
    clock_ns=time.perf_counter_ns,
    bundle_writer: BundleWriter | None = None,
) -> NoopDiagnosticSession | DiagnosticSession:
    if not policy.enabled:
        return NOOP_DIAGNOSTIC_SESSION
    return DiagnosticSession(
        policy,
        clock_ns=clock_ns,
        bundle_writer=bundle_writer,
    )
