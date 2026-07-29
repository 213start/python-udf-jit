from __future__ import annotations

import threading
import time
from collections import OrderedDict
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
        max_entries: int = 1024,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            type(ttl_ns) is not int
            or ttl_ns <= 0
            or type(max_entries) is not int
            or max_entries <= 0
        ):
            raise ValueError("invalid_negative_cache_ttl")
        self._ttl_ns = ttl_ns
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, NegativeEntry] = OrderedDict()
        self._lock = threading.Lock()

    def _purge_expired(self, now: int) -> None:
        for key, entry in tuple(self._entries.items()):
            if entry.expires_ns <= now:
                self._entries.pop(key, None)

    def get(self, key: str) -> NegativeEntry | None:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry

    def record(self, key: str, reason_code: str) -> NegativeEntry:
        if not key or not reason_code:
            raise ValueError("invalid_negative_cache_entry")
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
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
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return entry

    def clear(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def entry_count(self) -> int:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            return len(self._entries)
