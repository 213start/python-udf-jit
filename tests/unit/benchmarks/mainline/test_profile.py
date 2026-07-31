from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from python_udf_jit.benchmarks.mainline import (
    EnvironmentFingerprint,
    MainlineProfile,
    ProfileError,
    canonical_correctness_sha256,
    validate_profile_document,
)


ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = ROOT / "benchmarks/mainline/profile_schema.json"


class MainlineProfileTests(unittest.TestCase):
    def _environment(self) -> EnvironmentFingerprint:
        return EnvironmentFingerprint(
            python_version="3.14.3",
            cinderx_commit="a" * 40,
            cinderx_soabi="cpython-314-aarch64-linux-gnu",
            daft_version="0.7.2",
            ray_version="2.55.0",
            lance_version="7.0.0",
            pyarrow_version="22.0.0",
            machine="aarch64",
            cpu_model="test-cpu",
            support_matrix_sha256="b" * 64,
            policy_version="mainline-v1",
        )

    def test_profile_is_repeatable_and_hotspots_have_stable_order(self) -> None:
        profile = MainlineProfile(
            run_id="run-1",
            environment=self._environment(),
            correctness_sha256=canonical_correctness_sha256(
                {"rows": [1, None, 3], "total": 4}
            ),
        )
        profile.record_phase("execute", 30)
        profile.record_phase("compile", 20)
        profile.record_phase("execute", 10)
        profile.record_phase("capture", 20)

        first = profile.to_document()
        second = profile.to_document()

        self.assertEqual(first, second)
        self.assertEqual(
            [entry["phase"] for entry in first["hotspots"]],
            ["execute", "capture", "compile"],
        )
        self.assertEqual(
            canonical_correctness_sha256({"b": 2, "a": 1}),
            canonical_correctness_sha256({"a": 1, "b": 2}),
        )
        validate_profile_document(first)

    def test_poor_performance_does_not_fail_functional_completion(self) -> None:
        profile = MainlineProfile(
            run_id="run-1",
            environment=self._environment(),
            correctness_sha256="c" * 64,
        )
        profile.record_phase("end_to_end", 200)
        profile.assess_performance(
            baseline_ns=100,
            candidate_ns=200,
            reference_target_speedup=1.15,
        )

        document = profile.to_document()

        self.assertEqual(document["functional_status"], "pass")
        self.assertEqual(document["performance"]["mode"], "directional")
        self.assertEqual(
            document["performance"]["status"],
            "directional_recorded",
        )
        self.assertFalse(document["performance"]["blocks_functional_completion"])
        validate_profile_document(document)
        self.assertEqual(
            document["performance"]["conclusion_scope"],
            "directional_only",
        )
        self.assertNotIn(
            document["performance"]["status"],
            {"meets_target", "below_target"},
        )

    def test_profile_schema_is_closed_and_requires_correctness_and_fingerprint(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema["additionalProperties"], False)
        self.assertIn("environment", schema["required"])
        self.assertIn("correctness_sha256", schema["required"])
        self.assertIn("phase_timings", schema["required"])
        self.assertIn("hotspots", schema["required"])
        self.assertIn("diagnostics", schema["required"])

    def test_formal_performance_profile_requires_diagnostics_off(self) -> None:
        with self.assertRaisesRegex(
            ProfileError,
            "diagnostics_must_be_off",
        ):
            MainlineProfile(
                run_id="run-1",
                environment=self._environment(),
                correctness_sha256="c" * 64,
                diagnostic_profile="full",
            )

        profile = MainlineProfile(
            run_id="run-1",
            environment=self._environment(),
            correctness_sha256="c" * 64,
        )
        profile.record_phase("execute", 10)
        document = profile.to_document()
        self.assertEqual(document["diagnostics"], "off")
        document["diagnostics"] = "summary"
        with self.assertRaisesRegex(
            ProfileError,
            "diagnostics_must_be_off",
        ):
            validate_profile_document(document)

    def test_validator_rejects_environment_phase_and_performance_tampering(
        self,
    ) -> None:
        profile = MainlineProfile(
            run_id="run-1",
            environment=self._environment(),
            correctness_sha256="c" * 64,
        )
        profile.record_phase("execute", 100)
        profile.assess_performance(
            baseline_ns=200,
            candidate_ns=100,
            reference_target_speedup=1.15,
        )
        document = profile.to_document()

        invalid_environment = copy.deepcopy(document)
        invalid_environment["environment"]["cinderx_commit"] = "not-a-commit"
        identity = dict(invalid_environment["environment"])
        identity.pop("fingerprint_sha256")
        invalid_environment["environment"]["fingerprint_sha256"] = (
            hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest()
        )

        invalid_phase = copy.deepcopy(document)
        invalid_phase["phase_timings"][0]["sample_count"] = 2

        invalid_performance = copy.deepcopy(document)
        invalid_performance["performance"]["speedup"] = 0.5

        for name, changed in (
            ("environment", invalid_environment),
            ("phase", invalid_phase),
            ("performance", invalid_performance),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_profile_document(changed)


if __name__ == "__main__":
    unittest.main()
