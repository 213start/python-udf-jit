from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.bundle import (
    BundleRunContext,
    BundleStatus,
    open_bundle,
    read_bundle,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.session import (
    DiagnosticSession,
    NoopDiagnosticSession,
    open_diagnostic_session,
)


class _Clock:
    def __init__(self, *values: int) -> None:
        self.values = iter(values)
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return next(self.values)


class DiagnosticSessionTests(unittest.TestCase):
    def _active_policy(self, root: Path):
        return resolve_diagnostic_policy(
            {
                "UDFJIT_DIAGNOSTICS": "summary",
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "artifact:abc123",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            },
            DiagnosticRuntimeContext(
                workspace_root=root / "workspace",
                home_root=root / "home",
            ),
        )

    def test_off_session_never_calls_clock_or_creates_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = _Clock(1, 2)
            policy = resolve_diagnostic_policy(
                {},
                DiagnosticRuntimeContext(
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )

            session = open_diagnostic_session(policy, clock_ns=clock)
            self.assertIsInstance(session, NoopDiagnosticSession)
            with session.span("compile", "variant:abc"):
                pass
            self.assertFalse(session.record_metric("count", 1))
            self.assertFalse(session.record_nodes(({"node_id": "n1"},)))
            self.assertFalse(session.record_edges(({"from": "n1", "to": "n2"},)))
            self.assertIsNone(session.finalize(BundleStatus.COMPLETE))
            self.assertEqual(clock.calls, 0)
            self.assertEqual(tuple(root.iterdir()), ())

    def test_active_session_records_monotonic_stage_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = _Clock(100, 175)
            session = open_diagnostic_session(
                self._active_policy(root),
                clock_ns=clock,
            )

            self.assertIsInstance(session, DiagnosticSession)
            with session.span("python_compile", "variant:abc"):
                pass

            profiles = session.stage_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].stage, "python_compile")
            self.assertEqual(profiles[0].identity, "variant:abc")
            self.assertEqual(profiles[0].duration_ns, 75)
            self.assertEqual(clock.calls, 2)

    def test_span_does_not_suppress_business_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = open_diagnostic_session(
                self._active_policy(Path(temporary)),
                clock_ns=_Clock(10, 11),
            )
            with self.assertRaisesRegex(RuntimeError, "business-error"):
                with session.span("native_call", "variant:abc"):
                    raise RuntimeError("business-error")

    def test_policy_session_bundle_integration_finalizes_readable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = self._active_policy(root)
            writer = open_bundle(
                policy,
                BundleRunContext(
                    run_id="run-1",
                    runtime_mode="auto",
                    process_key="worker-1",
                ),
            )
            session = open_diagnostic_session(
                policy,
                bundle_writer=writer,
                clock_ns=_Clock(1, 3),
            )
            with session.span("compile", "variant:abc"):
                pass
            bundle_ref = session.finalize(BundleStatus.COMPLETE)

            self.assertIsNotNone(bundle_ref)
            loaded = read_bundle(bundle_ref.path)
            self.assertIs(loaded.status, BundleStatus.COMPLETE)
            self.assertEqual(loaded.manifest["diagnostic_policy_hash"], policy.sha256)


if __name__ == "__main__":
    unittest.main()
