from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, Iterator, TypeVar

from python_udf_jit.runtime.circuit_breaker import CircuitBreaker
from python_udf_jit.runtime.compile_pool import (
    CompileOutcome,
    CompilePool,
    CompileTimeoutError,
    SubmitDecision,
)
from python_udf_jit.runtime.negative_cache import NegativeCache
from python_udf_jit.runtime.process_governor import ProcessVariantGovernor
from python_udf_jit.runtime.variant import VariantKey, WorkerProcessKey


T = TypeVar("T")


@dataclass(frozen=True)
class VariantNamespace:
    job_id: str
    tenant_id: str

    def __post_init__(self) -> None:
        if not self.job_id or not self.tenant_id:
            raise ValueError("invalid_variant_namespace")


@dataclass(frozen=True)
class RuntimeVariant(Generic[T]):
    key: VariantKey
    value: T
    code_bytes: int


class ResolveKind(StrEnum):
    HIT = "hit"
    COMPILE_PENDING = "compile_pending"
    INTERPRET = "interpret"
    CIRCUIT_OPEN = "circuit_open"
    PROCESS_MISMATCH = "process_mismatch"


@dataclass(frozen=True)
class ResolveDecision(Generic[T]):
    kind: ResolveKind
    reason_code: str
    variant: RuntimeVariant[T] | None = None


class VariantManager(Generic[T]):
    """Bounded, isolated, asynchronous multi-variant runtime."""

    def __init__(
        self,
        *,
        process: WorkerProcessKey,
        namespace: VariantNamespace,
        max_variants: int,
        max_code_bytes: int,
        max_compile_workers: int = 1,
        max_pending_compiles: int = 8,
        compile_timeout_ns: int = 30_000_000_000,
        negative_ttl_ns: int = 30_000_000_000,
        max_negative_entries: int = 1024,
        circuit_failure_threshold: int = 3,
        circuit_reset_ns: int = 30_000_000_000,
        code_size: Callable[[T], int] | None = None,
        closer: Callable[[T], None] | None = None,
        process_governor: ProcessVariantGovernor | None = None,
        governor_owner: str | None = None,
    ) -> None:
        if process.pid != os.getpid():
            raise ValueError("variant_manager_process_mismatch")
        if (
            type(max_variants) is not int
            or max_variants <= 0
            or type(max_code_bytes) is not int
            or max_code_bytes <= 0
        ):
            raise ValueError("invalid_variant_budget")
        self.process = process
        self.namespace = namespace
        self._max_variants = max_variants
        self._max_code_bytes = max_code_bytes
        self._code_size = code_size or (
            lambda value: int(
                getattr(value, "code_size", sys.getsizeof(value))
            )
        )
        self._closer = closer
        if (process_governor is None) != (governor_owner is None):
            raise ValueError("incomplete_process_governor")
        self._process_governor = process_governor
        self._governor_owner = governor_owner
        self._pool = CompilePool[T](
            max_workers=max_compile_workers,
            max_pending=max_pending_compiles,
            compile_timeout_ns=compile_timeout_ns,
        )
        self._negative = NegativeCache(
            ttl_ns=negative_ttl_ns,
            max_entries=max_negative_entries,
        )
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_reset_ns = circuit_reset_ns
        self._breakers: dict[str, CircuitBreaker] = {}
        self._variants: OrderedDict[str, RuntimeVariant[T]] = OrderedDict()
        self._references: dict[str, int] = {}
        self._code_bytes = 0
        self._closed = False
        self._lock = threading.RLock()

    def _breaker(self, digest: str) -> CircuitBreaker:
        breaker = self._breakers.get(digest)
        if breaker is None:
            breaker = CircuitBreaker(
                failure_threshold=self._circuit_failure_threshold,
                reset_timeout_ns=self._circuit_reset_ns,
            )
            self._breakers[digest] = breaker
        return breaker

    def _discard(self, values: tuple[T, ...]) -> None:
        if self._closer is None:
            return
        for value in values:
            try:
                self._closer(value)
            except Exception:
                pass

    def resolve(
        self,
        key: VariantKey,
        compiler: Callable[[], T],
    ) -> ResolveDecision[T]:
        if key.process != self.process or os.getpid() != self.process.pid:
            return ResolveDecision(
                ResolveKind.PROCESS_MISMATCH,
                "worker_process_mismatch",
            )
        digest = key.sha256
        with self._lock:
            variant = self._variants.get(digest)
            if variant is not None:
                self._variants.move_to_end(digest)
                return ResolveDecision(
                    ResolveKind.HIT,
                    "variant_cache_hit",
                    variant,
                )
            negative = self._negative.get(digest)
            if negative is not None:
                return ResolveDecision(
                    ResolveKind.INTERPRET,
                    "negative_cache",
                )
            breaker = self._breakers.get(digest)
            if breaker is not None and breaker.state().open:
                return ResolveDecision(
                    ResolveKind.CIRCUIT_OPEN,
                    "circuit_open",
                )

        def completed(outcome: CompileOutcome[T]) -> None:
            if outcome.error is not None:
                reason = (
                    "compile_timeout"
                    if isinstance(outcome.error, CompileTimeoutError)
                    else "compile_failed"
                )
                self._negative.record(digest, reason)
                with self._lock:
                    self._breaker(digest).record_internal_failure(reason)
                return
            value = outcome.value
            if value is None:
                reason = "compile_failed"
                self._negative.record(digest, reason)
                with self._lock:
                    self._breaker(digest).record_internal_failure(reason)
                return
            try:
                size = self._code_size(value)
            except Exception:
                size = 0
            if type(size) is not int or size <= 0 or size > self._max_code_bytes:
                reason = "compile_rejected_code_budget"
                self._negative.record(digest, reason)
                self._discard((value,))
                return
            with self._lock:
                published, discarded = self._publish(
                    RuntimeVariant(key, value, size)
                )
            self._discard(discarded)
            if not published:
                self._negative.record(
                    digest,
                    "compile_rejected_code_budget",
                )
                return
            self._negative.clear(digest)
            with self._lock:
                breaker = self._breakers.pop(digest, None)
                if breaker is not None:
                    breaker.record_success()

        submit = self._pool.submit(digest, compiler, completed)
        if submit is SubmitDecision.SUBMITTED:
            return ResolveDecision(
                ResolveKind.COMPILE_PENDING,
                "compile_submitted",
            )
        if submit is SubmitDecision.INFLIGHT:
            return ResolveDecision(
                ResolveKind.INTERPRET,
                "compile_inflight",
            )
        if submit is SubmitDecision.CLOSED:
            reason = "compile_pool_closed"
        else:
            reason = "compile_capacity_exhausted"
        return ResolveDecision(
            ResolveKind.INTERPRET,
            reason,
        )

    def _publish(
        self,
        variant: RuntimeVariant[T],
    ) -> tuple[bool, tuple[T, ...]]:
        digest = variant.key.sha256
        if self._closed:
            return False, (variant.value,)
        previous = self._variants.get(digest)
        if previous is not None and self._references.get(digest, 0):
            return False, (variant.value,)

        projected_count = len(self._variants) - int(previous is not None) + 1
        projected_bytes = (
            self._code_bytes
            - (0 if previous is None else previous.code_bytes)
            + variant.code_bytes
        )
        victims: list[str] = []
        for candidate in self._variants:
            if (
                projected_count <= self._max_variants
                and projected_bytes <= self._max_code_bytes
            ):
                break
            if (
                candidate == digest
                or self._references.get(candidate, 0)
            ):
                continue
            victim = self._variants[candidate]
            victims.append(candidate)
            projected_count -= 1
            projected_bytes -= victim.code_bytes
        if (
            projected_count > self._max_variants
            or projected_bytes > self._max_code_bytes
        ):
            return False, (variant.value,)
        governor_removals = tuple(
            value
            for value in (
                digest if previous is not None else None,
                *victims,
            )
            if value is not None
        )
        if (
            self._process_governor is not None
            and self._governor_owner is not None
            and not self._process_governor.replace(
                self._governor_owner,
                digest=digest,
                code_bytes=variant.code_bytes,
                removals=governor_removals,
            )
        ):
            return False, (variant.value,)

        discarded: list[T] = []
        if previous is not None:
            removed = self._variants.pop(digest)
            self._code_bytes -= removed.code_bytes
            discarded.append(removed.value)
        for victim in victims:
            removed = self._variants.pop(victim)
            self._code_bytes -= removed.code_bytes
            discarded.append(removed.value)
        self._variants[digest] = variant
        self._code_bytes += variant.code_bytes
        return True, tuple(discarded)

    @contextmanager
    def acquire(self, key: VariantKey) -> Iterator[T]:
        digest = key.sha256
        with self._lock:
            variant = self._variants.get(digest)
            if variant is None:
                raise KeyError("variant_not_active")
            self._references[digest] = self._references.get(digest, 0) + 1
        try:
            yield variant.value
        finally:
            with self._lock:
                remaining = self._references[digest] - 1
                if remaining:
                    self._references[digest] = remaining
                else:
                    self._references.pop(digest, None)

    def active_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._variants)

    def budget_state(self) -> tuple[int, int]:
        with self._lock:
            return len(self._variants), self._code_bytes

    def budget_limits(self) -> tuple[int, int]:
        return self._max_variants, self._max_code_bytes

    def can_retire(self) -> bool:
        with self._lock:
            return not self._references and self._pool.inflight_count() == 0

    def drain(self) -> None:
        self._pool.drain()

    def close(self, closer: Callable[[T], None] | None = None) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)
        with self._lock:
            values = [variant.value for variant in self._variants.values()]
            self._variants.clear()
            self._references.clear()
            self._breakers.clear()
            self._code_bytes = 0
        if (
            self._process_governor is not None
            and self._governor_owner is not None
        ):
            self._process_governor.release(self._governor_owner)
        effective_closer = closer or self._closer
        if effective_closer is not None:
            for value in values:
                effective_closer(value)
