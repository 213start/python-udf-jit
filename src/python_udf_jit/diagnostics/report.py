from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
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
