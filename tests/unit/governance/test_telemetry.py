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
        artifact_sha256="c" * 64,
        variant_sha256="d" * 64,
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

    def test_close_is_ordered_after_an_in_progress_emit(self) -> None:
        delivered: list[GovernanceEvent] = []
        telemetry = AsyncTelemetry(delivered.append, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        original = telemetry._queue.put_nowait

        def delayed_put(event: GovernanceEvent) -> None:
            entered.set()
            release.wait()
            original(event)

        telemetry._queue.put_nowait = delayed_put
        emitter = threading.Thread(target=lambda: telemetry.try_emit(_event()))
        closer = threading.Thread(target=telemetry.close)
        emitter.start()
        self.assertTrue(entered.wait(1))
        closer.start()
        self.assertTrue(closer.is_alive())
        release.set()
        emitter.join(1)
        closer.join(1)

        self.assertFalse(emitter.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(delivered, [_event()])
        self.assertEqual(telemetry.counters().accepted, 1)
        self.assertFalse(telemetry.try_emit(_event()))
        telemetry.close()
        self.assertEqual(telemetry.counters().dropped, 1)

    def test_event_rejects_unversioned_categories_and_raw_identities(
        self,
    ) -> None:
        for field, value in (
            ("stage", "private-stage"),
            ("decision", "private-decision"),
            ("reason_code", "customer-42"),
            ("source_identity", "private.module"),
            ("artifact_sha256", "private-artifact"),
            ("variant_sha256", "private-variant"),
        ):
            values = dict(_event().__dict__)
            values[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    GovernanceEvent(**values)
