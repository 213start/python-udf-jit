from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DecisionEvent:
    """A value-free event suitable for local piercing evidence."""

    stage: str
    decision: str
    reason_code: str
    candidate_id: str = ""
    timestamp_ns: int = field(default_factory=time.time_ns)


_EVENTS: deque[DecisionEvent] = deque(maxlen=4096)
_EVENTS_LOCK = threading.Lock()


def try_emit(event: DecisionEvent) -> bool:
    """Best-effort emission; diagnostics must never become a semantic dependency."""

    try:
        with _EVENTS_LOCK:
            _EVENTS.append(event)
    except Exception:
        return False
    return True


def snapshot_events() -> tuple[DecisionEvent, ...]:
    with _EVENTS_LOCK:
        return tuple(_EVENTS)


def clear_events() -> None:
    with _EVENTS_LOCK:
        _EVENTS.clear()
