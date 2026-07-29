from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable


def _safe_identity(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or any(character in value for character in ("/", "\\", "\n", "\r"))
    ):
        raise ValueError(f"unsafe_{field}")
    return value


@dataclass(frozen=True)
class GovernanceEvent:
    """Value-free region/batch event; no arbitrary payload field exists."""

    run_id: str
    job_id: str
    tenant_id: str
    policy_sha256: str
    stage: str
    decision: str
    reason_code: str
    source_identity: str
    count: int = 1
    duration_ns: int = 0

    def __post_init__(self) -> None:
        for field in (
            "run_id",
            "job_id",
            "tenant_id",
            "policy_sha256",
            "stage",
            "decision",
            "reason_code",
            "source_identity",
        ):
            _safe_identity(getattr(self, field), field)
        if len(self.policy_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.policy_sha256
        ):
            raise ValueError("unsafe_policy_sha256")
        if (
            type(self.count) is not int
            or self.count < 0
            or type(self.duration_ns) is not int
            or self.duration_ns < 0
        ):
            raise ValueError("unsafe_event_counter")

    @property
    def document(self) -> dict[str, object]:
        return {
            "count": self.count,
            "decision": self.decision,
            "duration_ns": self.duration_ns,
            "job_id": self.job_id,
            "policy_sha256": self.policy_sha256,
            "reason_code": self.reason_code,
            "run_id": self.run_id,
            "source_identity": self.source_identity,
            "stage": self.stage,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class TelemetryCounters:
    accepted: int
    delivered: int
    dropped: int
    backend_failures: int


class AsyncTelemetry:
    """Bounded non-blocking telemetry; backend failure is diagnostic only."""

    def __init__(
        self,
        backend: Callable[[GovernanceEvent], None],
        *,
        capacity: int,
    ) -> None:
        if not callable(backend) or type(capacity) is not int or capacity <= 0:
            raise ValueError("invalid_telemetry_configuration")
        self._backend = backend
        self._queue: queue.Queue[GovernanceEvent | None] = queue.Queue(
            maxsize=capacity
        )
        self._lock = threading.Lock()
        self._accepted = 0
        self._delivered = 0
        self._dropped = 0
        self._backend_failures = 0
        self._closed = False
        self._worker = threading.Thread(
            target=self._consume,
            name="udfjit-telemetry",
            daemon=True,
        )
        self._worker.start()

    def try_emit(self, event: GovernanceEvent) -> bool:
        if not isinstance(event, GovernanceEvent):
            return False
        with self._lock:
            if self._closed:
                self._dropped += 1
                return False
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        with self._lock:
            self._accepted += 1
        return True

    def _consume(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                try:
                    self._backend(event)
                except BaseException:
                    with self._lock:
                        self._backend_failures += 1
                else:
                    with self._lock:
                        self._delivered += 1
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        self._queue.join()

    def counters(self) -> TelemetryCounters:
        with self._lock:
            return TelemetryCounters(
                self._accepted,
                self._delivered,
                self._dropped,
                self._backend_failures,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._queue.join()
        self._worker.join()
