from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.config import (
    DiagnosticConfigurationError,
    DiagnosticPerfMode,
    DiagnosticProfile,
    DiagnosticRuntimeContext,
    DiagnosticSourcePolicy,
    resolve_diagnostic_policy,
)


class DiagnosticPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.output = self.root / "diagnostics"
        self.home.mkdir()
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _context(self, *, dedicated_worker: bool = False) -> DiagnosticRuntimeContext:
        return DiagnosticRuntimeContext(
            dedicated_worker=dedicated_worker,
            workspace_root=self.workspace,
            home_root=self.home,
        )

    def _environment(self, profile: str = "summary") -> dict[str, str]:
        return {
            "UDFJIT_DIAGNOSTICS": profile,
            "UDFJIT_DIAGNOSTIC_DIR": os.fspath(self.output),
            "UDFJIT_DIAGNOSTIC_FILTER": "artifact:abc123",
            "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
            "UDFJIT_DIAGNOSTIC_PERF": "off",
            "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "0.25",
            "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
        }

    def test_unset_profile_resolves_to_immutable_off_without_output(self) -> None:
        policy = resolve_diagnostic_policy({}, self._context())

        self.assertIs(policy.profile, DiagnosticProfile.OFF)
        self.assertIsNone(policy.output_root)
        self.assertEqual(policy.selector, "")
        self.assertIs(policy.source_policy, DiagnosticSourcePolicy.RANGES)
        self.assertIs(policy.perf_mode, DiagnosticPerfMode.OFF)
        self.assertFalse(policy.requires_dedicated_worker)
        self.assertEqual(
            policy.sha256,
            hashlib.sha256(
                json.dumps(
                    policy.document,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest(),
        )
        self.assertFalse(self.output.exists())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.sample_rate = 1.0  # type: ignore[misc]

    def test_importing_diagnostics_package_does_not_load_deep_diagnostics(
        self,
    ) -> None:
        command = (
            "import sys; import python_udf_jit.diagnostics; "
            "print(int(any(name in sys.modules for name in "
            "('python_udf_jit.diagnostics.bundle', "
            "'python_udf_jit.diagnostics.config', "
            "'python_udf_jit.diagnostics.session'))))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "0")

    def test_summary_parses_and_hashes_a_frozen_snapshot(self) -> None:
        first = resolve_diagnostic_policy(
            self._environment(),
            self._context(),
        )
        second = resolve_diagnostic_policy(
            dict(reversed(tuple(self._environment().items()))),
            self._context(),
        )

        self.assertIs(first.profile, DiagnosticProfile.SUMMARY)
        self.assertEqual(first.output_root, self.output)
        self.assertEqual(first.sample_rate, 0.25)
        self.assertEqual(first.max_bytes, 1048576)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.sha256), 64)
        self.assertFalse(self.output.exists())

    def test_sample_rate_accepts_closed_interval_boundaries(self) -> None:
        for value in ("0", "1"):
            with self.subTest(value=value):
                environment = self._environment()
                environment["UDFJIT_DIAGNOSTIC_SAMPLE_RATE"] = value
                policy = resolve_diagnostic_policy(environment, self._context())
                self.assertEqual(policy.sample_rate, float(value))

    def test_selector_requires_a_supported_kind_and_value(self) -> None:
        for selector in ("unknown:value", "artifact", "candidate:"):
            with self.subTest(selector=selector):
                environment = self._environment()
                environment["UDFJIT_DIAGNOSTIC_FILTER"] = selector
                with self.assertRaisesRegex(
                    ValueError,
                    "filter_invalid",
                ):
                    resolve_diagnostic_policy(environment, self._context())

    def test_full_requires_dedicated_worker_and_supports_perf(self) -> None:
        environment = self._environment("full")
        environment["UDFJIT_DIAGNOSTIC_PERF"] = "record"

        with self.assertRaisesRegex(
            DiagnosticConfigurationError,
            "diagnostics_configuration_invalid",
        ):
            resolve_diagnostic_policy(environment, self._context())

        policy = resolve_diagnostic_policy(
            environment,
            self._context(dedicated_worker=True),
        )
        self.assertIs(policy.profile, DiagnosticProfile.FULL)
        self.assertIs(policy.perf_mode, DiagnosticPerfMode.RECORD)
        self.assertTrue(policy.requires_dedicated_worker)

    def test_summary_rejects_perf_record(self) -> None:
        environment = self._environment()
        environment["UDFJIT_DIAGNOSTIC_PERF"] = "record"
        with self.assertRaisesRegex(
            DiagnosticConfigurationError,
            "diagnostics_configuration_invalid",
        ):
            resolve_diagnostic_policy(environment, self._context())

    def test_explicit_diagnostics_reject_unsafe_output_roots(self) -> None:
        symlink = self.root / "linked-output"
        symlink.symlink_to(self.root / "real-output", target_is_directory=True)
        cases = (
            "relative/output",
            os.path.abspath(os.sep),
            os.fspath(self.home),
            os.fspath(self.workspace),
            os.fspath(symlink),
        )

        for output in cases:
            with self.subTest(output=output):
                environment = self._environment()
                environment["UDFJIT_DIAGNOSTIC_DIR"] = output
                with self.assertRaisesRegex(
                    DiagnosticConfigurationError,
                    "diagnostics_configuration_invalid",
                ):
                    resolve_diagnostic_policy(environment, self._context())

    def test_explicit_diagnostics_fail_closed_for_invalid_values(self) -> None:
        cases = (
            ("UDFJIT_DIAGNOSTICS", "verbose"),
            ("UDFJIT_DIAGNOSTIC_FILTER", ""),
            ("UDFJIT_DIAGNOSTIC_FILTER", "artifact:abc\nsecret"),
            ("UDFJIT_DIAGNOSTIC_SOURCE", "values"),
            ("UDFJIT_DIAGNOSTIC_PERF", "yes"),
            ("UDFJIT_DIAGNOSTIC_SAMPLE_RATE", "-0.1"),
            ("UDFJIT_DIAGNOSTIC_SAMPLE_RATE", "1.1"),
            ("UDFJIT_DIAGNOSTIC_SAMPLE_RATE", "nan"),
            ("UDFJIT_DIAGNOSTIC_MAX_BYTES", "0"),
            ("UDFJIT_DIAGNOSTIC_MAX_BYTES", str(2**31)),
        )

        for name, value in cases:
            with self.subTest(name=name, value=value):
                environment = self._environment()
                environment[name] = value
                with self.assertRaisesRegex(
                    DiagnosticConfigurationError,
                    "diagnostics_configuration_invalid",
                ):
                    resolve_diagnostic_policy(environment, self._context())


if __name__ == "__main__":
    unittest.main()
