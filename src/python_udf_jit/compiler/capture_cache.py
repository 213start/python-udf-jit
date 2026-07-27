from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.capture_verifier import verify_captured_program


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class CaptureCacheKey:
    job_namespace: str
    code_sha256: str
    dependency_sha256: str
    source_namespace_sha256: str
    schema_sha256: str
    adapter_abi_sha256: str
    policy_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.job_namespace) is not str
            or not self.job_namespace
            or len(self.job_namespace.encode("utf-8")) > 256
            or any(
                not _valid_digest(value)
                for value in (
                    self.code_sha256,
                    self.dependency_sha256,
                    self.source_namespace_sha256,
                    self.schema_sha256,
                    self.adapter_abi_sha256,
                    self.policy_sha256,
                )
            )
        ):
            raise ValueError("invalid capture cache key")


@dataclass(frozen=True)
class _CacheEntry:
    value: CapturedProgram
    expires_at: float


class CaptureCache:
    """Process-local, job-isolated bounded LRU for verified capture results."""

    def __init__(
        self,
        *,
        capacity: int = 256,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(capacity) is not int
            or capacity <= 0
            or type(ttl_seconds) not in {int, float}
            or ttl_seconds <= 0
        ):
            raise ValueError("invalid capture cache bounds")
        self._capacity = capacity
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._entries: OrderedDict[
            CaptureCacheKey,
            _CacheEntry,
        ] = OrderedDict()
        self._lock = threading.RLock()

    def _purge_expired_locked(self, now: float) -> int:
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def get(self, key: CaptureCacheKey) -> CapturedProgram | None:
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            verify_captured_program(entry.value)
            self._entries.move_to_end(key)
            return entry.value

    def put(
        self,
        key: CaptureCacheKey,
        value: CapturedProgram,
    ) -> None:
        if (
            key.code_sha256 != value.identities.code.sha256
            or key.dependency_sha256
            != value.identities.dependency.sha256
            or key.source_namespace_sha256
            != value.identities.source.namespace_sha256
        ):
            raise ValueError("capture cache identity mismatch")
        verify_captured_program(value)
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            self._entries[key] = _CacheEntry(
                value,
                now + self._ttl_seconds,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear_job(self, job_namespace: str) -> int:
        with self._lock:
            keys = [
                key
                for key in self._entries
                if key.job_namespace == job_namespace
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked(self._clock())

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked(self._clock())
            return len(self._entries)
