from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class NegativeEntry:
    reason_code: str
    expires_ns: int
    failure_count: int


class NegativeCache:
    """Exact-key compile rejection cache with a monotonic TTL."""

    def __init__(
        self,
        *,
        ttl_ns: int,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(ttl_ns) is not int or ttl_ns <= 0:
            raise ValueError("invalid_negative_cache_ttl")
        self._ttl_ns = ttl_ns
        self._clock = clock
        self._entries: dict[str, NegativeEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> NegativeEntry | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_ns <= now:
                self._entries.pop(key, None)
                return None
            return entry

    def record(self, key: str, reason_code: str) -> NegativeEntry:
        if not key or not reason_code:
            raise ValueError("invalid_negative_cache_entry")
        now = self._clock()
        with self._lock:
            previous = self._entries.get(key)
            count = (
                1
                if previous is None or previous.expires_ns <= now
                else previous.failure_count + 1
            )
            entry = NegativeEntry(
                reason_code=reason_code,
                expires_ns=now + self._ttl_ns,
                failure_count=count,
            )
            self._entries[key] = entry
            return entry

    def clear(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
