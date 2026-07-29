from __future__ import annotations

import threading
import unittest

from python_udf_jit.runtime.compile_pool import (
    CompilePool,
    SubmitDecision,
)


class CompileSingleflightTests(unittest.TestCase):
    def test_identical_requests_share_one_bounded_compile(self) -> None:
        pool = CompilePool[str](max_workers=1, max_pending=0)
        entered = threading.Event()
        release = threading.Event()
        outcomes = []
        calls = 0

        def compile_once() -> str:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait()
            return "code"

        try:
            first = pool.submit("same", compile_once, outcomes.append)
            entered.wait()
            decisions = [
                pool.submit("same", compile_once, outcomes.append)
                for _ in range(99)
            ]
            other = pool.submit("other", lambda: "other", outcomes.append)
            release.set()
            pool.drain()
        finally:
            pool.shutdown()

        self.assertEqual(first, SubmitDecision.SUBMITTED)
        self.assertEqual(set(decisions), {SubmitDecision.INFLIGHT})
        self.assertEqual(other, SubmitDecision.CAPACITY_EXHAUSTED)
        self.assertEqual(calls, 1)
        self.assertEqual([outcome.value for outcome in outcomes], ["code"])
