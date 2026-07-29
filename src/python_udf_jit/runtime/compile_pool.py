from __future__ import annotations

import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, TypeVar, cast


T = TypeVar("T")


class SubmitDecision(StrEnum):
    SUBMITTED = "submitted"
    INFLIGHT = "inflight"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    CLOSED = "closed"


@dataclass(frozen=True)
class CompileOutcome(Generic[T]):
    value: T | None = None
    error: BaseException | None = None


class CompileTimeoutError(TimeoutError):
    """A compiler invocation exceeded the frozen runtime budget."""


class CompilePool(Generic[T]):
    """Bounded process-local compiler pool with exact-key singleflight."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_pending: int,
        compile_timeout_ns: int = 30_000_000_000,
    ) -> None:
        if (
            type(max_workers) is not int
            or max_workers <= 0
            or type(max_pending) is not int
            or max_pending < 0
            or type(compile_timeout_ns) is not int
            or compile_timeout_ns <= 0
        ):
            raise ValueError("invalid_compile_pool_budget")
        self._compile_timeout_seconds = compile_timeout_ns / 1_000_000_000
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="udfjit-compile",
        )
        self._capacity = threading.BoundedSemaphore(
            max_workers + max_pending
        )
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._inflight: dict[str, Future[T]] = {}
        self._closed = False

    def _run_with_timeout(self, compiler: Callable[[], T]) -> T:
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, compiler()))
            except BaseException as error:
                result.put((False, error))

        worker = threading.Thread(
            target=invoke,
            name="udfjit-compile-invocation",
            daemon=True,
        )
        worker.start()
        try:
            succeeded, value = result.get(
                timeout=self._compile_timeout_seconds
            )
        except queue.Empty as error:
            raise CompileTimeoutError("compile_timeout") from error
        if succeeded:
            return cast(T, value)
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("compile_outcome_invalid")

    def submit(
        self,
        key: str,
        compiler: Callable[[], T],
        completed: Callable[[CompileOutcome[T]], None],
    ) -> SubmitDecision:
        if not isinstance(key, str) or not key or not callable(compiler):
            raise ValueError("invalid_compile_request")
        with self._lock:
            if self._closed:
                return SubmitDecision.CLOSED
            if key in self._inflight:
                return SubmitDecision.INFLIGHT
            if not self._capacity.acquire(blocking=False):
                return SubmitDecision.CAPACITY_EXHAUSTED
            try:
                future = self._executor.submit(
                    self._run_with_timeout,
                    compiler,
                )
            except BaseException:
                self._capacity.release()
                raise
            self._inflight[key] = future

        def finish(done: Future[T]) -> None:
            try:
                try:
                    outcome = CompileOutcome(value=done.result())
                except BaseException as error:
                    outcome = CompileOutcome[T](error=error)
                completed(outcome)
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                    self._idle.notify_all()
                self._capacity.release()

        future.add_done_callback(finish)
        return SubmitDecision.SUBMITTED

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def drain(self) -> None:
        with self._idle:
            self._idle.wait_for(lambda: not self._inflight)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
