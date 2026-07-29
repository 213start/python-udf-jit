from __future__ import annotations

import threading
import unittest

from python_udf_jit.governance.telemetry import (
    AsyncTelemetry,
    GovernanceEvent,
)


def _event() -> GovernanceEvent:
    return GovernanceEvent(
        run_id="run-a",
        job_id="job-a",
        tenant_id="tenant-a",
        policy_sha256="a" * 64,
        stage="variant",
        decision="hit",
        reason_code="variant_cache_hit",
        source_identity="b" * 64,
    )


class AsyncTelemetryTests(unittest.TestCase):
    def test_full_queue_drops_without_blocking_the_caller(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def backend(_event: GovernanceEvent) -> None:
            entered.set()
            release.wait()

        telemetry = AsyncTelemetry(backend, capacity=1)
        try:
            self.assertTrue(telemetry.try_emit(_event()))
            entered.wait()
            self.assertTrue(telemetry.try_emit(_event()))
            self.assertFalse(telemetry.try_emit(_event()))
            release.set()
            telemetry.flush()
            counters = telemetry.counters()
        finally:
            telemetry.close()

        self.assertEqual(counters.accepted, 2)
        self.assertEqual(counters.delivered, 2)
        self.assertEqual(counters.dropped, 1)

    def test_backend_failure_is_counted_and_never_propagated(self) -> None:
        telemetry = AsyncTelemetry(
            lambda _event: (_ for _ in ()).throw(RuntimeError("backend")),
            capacity=1,
        )
        try:
            self.assertTrue(telemetry.try_emit(_event()))
            telemetry.flush()
            counters = telemetry.counters()
        finally:
            telemetry.close()

        self.assertEqual(counters.backend_failures, 1)
        self.assertEqual(counters.delivered, 0)
