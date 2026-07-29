from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitState:
    open: bool
    consecutive_internal_failures: int
    reason_code: str


class CircuitBreaker:
    """Namespace-local breaker; user exceptions never enter this API."""

    def __init__(self, *, failure_threshold: int) -> None:
        if type(failure_threshold) is not int or failure_threshold <= 0:
            raise ValueError("invalid_circuit_threshold")
        self._threshold = failure_threshold
        self._failures = 0
        self._reason = ""
        self._lock = threading.Lock()

    def record_internal_failure(self, reason_code: str) -> CircuitState:
        if not reason_code:
            raise ValueError("invalid_circuit_reason")
        with self._lock:
            self._failures += 1
            self._reason = reason_code
            return self._state()

    def record_success(self) -> CircuitState:
        with self._lock:
            self._failures = 0
            self._reason = ""
            return self._state()

    def state(self) -> CircuitState:
        with self._lock:
            return self._state()

    def _state(self) -> CircuitState:
        return CircuitState(
            open=self._failures >= self._threshold,
            consecutive_internal_failures=self._failures,
            reason_code=self._reason,
        )
