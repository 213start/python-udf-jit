from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CircuitState:
    open: bool
    half_open: bool
    consecutive_internal_failures: int
    reason_code: str


class CircuitBreaker:
    """Namespace-local breaker; user exceptions never enter this API."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_timeout_ns: int = 30_000_000_000,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            type(failure_threshold) is not int
            or failure_threshold <= 0
            or type(reset_timeout_ns) is not int
            or reset_timeout_ns <= 0
        ):
            raise ValueError("invalid_circuit_threshold")
        self._threshold = failure_threshold
        self._reset_timeout_ns = reset_timeout_ns
        self._clock = clock
        self._failures = 0
        self._reason = ""
        self._opened_ns: int | None = None
        self._lock = threading.Lock()

    def record_internal_failure(self, reason_code: str) -> CircuitState:
        if not reason_code:
            raise ValueError("invalid_circuit_reason")
        with self._lock:
            self._failures += 1
            self._reason = reason_code
            if self._failures >= self._threshold:
                self._opened_ns = self._clock()
            return self._state()

    def record_success(self) -> CircuitState:
        with self._lock:
            self._failures = 0
            self._reason = ""
            self._opened_ns = None
            return self._state()

    def state(self) -> CircuitState:
        with self._lock:
            return self._state()

    def _state(self) -> CircuitState:
        threshold_reached = self._failures >= self._threshold
        reset_elapsed = (
            threshold_reached
            and self._opened_ns is not None
            and self._clock() - self._opened_ns
            >= self._reset_timeout_ns
        )
        return CircuitState(
            open=threshold_reached and not reset_elapsed,
            half_open=threshold_reached and reset_elapsed,
            consecutive_internal_failures=self._failures,
            reason_code=self._reason,
        )
