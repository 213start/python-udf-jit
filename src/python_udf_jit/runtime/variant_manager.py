from __future__ import annotations

import os
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
    SubmitDecision,
)
from python_udf_jit.runtime.negative_cache import NegativeCache
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
        negative_ttl_ns: int = 30_000_000_000,
        circuit_failure_threshold: int = 3,
        code_size: Callable[[T], int] | None = None,
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
            lambda value: int(getattr(value, "code_size", 1))
        )
        self._pool = CompilePool[T](
            max_workers=max_compile_workers,
            max_pending=max_pending_compiles,
        )
        self._negative = NegativeCache(ttl_ns=negative_ttl_ns)
        self._breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold
        )
        self._variants: OrderedDict[str, RuntimeVariant[T]] = OrderedDict()
        self._references: dict[str, int] = {}
        self._code_bytes = 0
        self._lock = threading.RLock()

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
            state = self._breaker.state()
            if state.open:
                return ResolveDecision(
                    ResolveKind.CIRCUIT_OPEN,
                    state.reason_code or "internal_failure_budget_exhausted",
                )
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
                    f"negative_cache:{negative.reason_code}",
                )

        def completed(outcome: CompileOutcome[T]) -> None:
            if outcome.error is not None:
                reason = f"compile_failed:{type(outcome.error).__name__}"
                self._negative.record(digest, reason)
                self._breaker.record_internal_failure(reason)
                return
            value = outcome.value
            if value is None:
                reason = "compile_failed:empty_result"
                self._negative.record(digest, reason)
                self._breaker.record_internal_failure(reason)
                return
            size = self._code_size(value)
            if type(size) is not int or size <= 0 or size > self._max_code_bytes:
                reason = "compile_rejected:code_budget"
                self._negative.record(digest, reason)
                self._breaker.record_internal_failure(reason)
                return
            with self._lock:
                self._publish(RuntimeVariant(key, value, size))
            self._negative.clear(digest)
            self._breaker.record_success()

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
        return ResolveDecision(
            ResolveKind.INTERPRET,
            f"compile_{submit.value}",
        )

    def _publish(self, variant: RuntimeVariant[T]) -> None:
        digest = variant.key.sha256
        previous = self._variants.pop(digest, None)
        if previous is not None:
            self._code_bytes -= previous.code_bytes
        self._variants[digest] = variant
        self._code_bytes += variant.code_bytes
        self._evict()

    def _evict(self) -> None:
        while (
            len(self._variants) > self._max_variants
            or self._code_bytes > self._max_code_bytes
        ):
            victim = next(iter(self._variants), None)
            if (
                victim is None
                or self._references.get(victim, 0) != 0
            ):
                return
            removed = self._variants.pop(victim)
            self._code_bytes -= removed.code_bytes

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
                self._evict()

    def active_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._variants)

    def drain(self) -> None:
        self._pool.drain()

    def close(self, closer: Callable[[T], None] | None = None) -> None:
        self._pool.shutdown(wait=True)
        with self._lock:
            values = [variant.value for variant in self._variants.values()]
            self._variants.clear()
            self._references.clear()
            self._code_bytes = 0
        if closer is not None:
            for value in values:
                closer(value)
