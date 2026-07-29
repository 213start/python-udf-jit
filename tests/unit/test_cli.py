from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python_udf_jit.benchmarks.mainline import (
    EnvironmentFingerprint,
    MainlineProfile,
)
from python_udf_jit.cli import main
from python_udf_jit.governance.explain import build_explain_report
from python_udf_jit.governance.telemetry import GovernanceEvent
from python_udf_jit.protocol.codec import encode_artifact
from tests.unit.protocol.test_artifact_codec import artifact


class CliTests(unittest.TestCase):
    def test_artifact_verify_is_private_and_never_maps_machine_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.udfjit"
            path.write_bytes(encode_artifact(artifact()))
            path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["artifact", "verify", str(path)])

        document = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(document["status"], "pass")
        self.assertFalse(document["machine_code_mapped"])
        self.assertEqual(
            (document["format_major"], document["format_minor"]),
            (1, 0),
        )
        self.assertEqual(document["schema_version"], 1)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.udfjit"
            path.write_bytes(encode_artifact(artifact()))
            path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["artifact", "inspect", str(path)])
        inspected = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(inspected["operation_count"], 6)
        self.assertEqual(inspected["region_count"], 1)
        self.assertFalse(inspected["machine_code_mapped"])

    def test_artifact_verify_rejects_symlink_and_public_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "artifact.udfjit"
            target.write_bytes(encode_artifact(artifact()))
            target.chmod(0o644)
            link = root / "link.udfjit"
            link.symlink_to(target)
            for path in (target, link):
                with self.subTest(path=path.name):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        status = main(
                            ["artifact", "verify", os.fspath(path)]
                        )
                    self.assertEqual(status, 2)
                    self.assertEqual(
                        json.loads(output.getvalue())["reason_code"],
                        "local_file_not_authorized",
                    )
                    self.assertNotIn(
                        os.fspath(path),
                        output.getvalue(),
                    )
                    self.assertEqual(
                        json.loads(output.getvalue())["status"],
                        "fail",
                    )

    def test_explain_strictly_rebuilds_the_scoped_report(self) -> None:
        report = build_explain_report(
            [
                GovernanceEvent(
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
            ],
            run_id="run-a",
            job_id="job-a",
            tenant_id="tenant-a",
            policy_sha256="a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explain.json"
            path.write_text(json.dumps(report), encoding="ascii")
            path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["explain", str(path)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue()), report)

            report["private_business_value"] = "customer-42"
            path.write_text(json.dumps(report), encoding="ascii")
            path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["explain", str(path)])
            self.assertEqual(status, 2)
            self.assertEqual(
                json.loads(output.getvalue())["reason_code"],
                "explain_report_invalid",
            )
            self.assertNotIn("customer-42", output.getvalue())

    def test_compatibility_and_mainline_benchmark_are_versioned(
        self,
    ) -> None:
        manifest = {
            "schema_version": 1,
            "artifact_kind": "scalar-piercing-environment-contract",
            "profile": "baseline",
            "plugin_mode": "off",
            "locked_versions": {
                "python": "3.14.3",
                "daft": "0.7.2",
                "ray": "2.55.0",
                "pyarrow": "22.0.0",
            },
            "non_blocking_versions": {"lance": "7.0.0"},
            "required_fingerprints": [
                "container_image_digest",
                "python_version",
                "cinderx_commit",
                "cinderx_base_image_digest",
                "cinderx_wheel_sha256",
                "cinderx_soabi",
                "daft_version",
                "ray_version",
                "pyarrow_version",
                "udf_jit_wheel_sha256",
            ],
            "compatibility_policy": {
                "ray_daft_mismatch": "stop",
                "local_ray_fallback_allowed": False,
            },
        }
        environment = EnvironmentFingerprint(
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
            policy_version="mainline",
        )
        profile = MainlineProfile(
            run_id="run-a",
            environment=environment,
            correctness_sha256="c" * 64,
        )
        profile.record_phase("execute", 10)
        profile.assess_performance(
            baseline_ns=100,
            candidate_ns=200,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "compatibility.json"
            manifest_path.write_text(json.dumps(manifest), encoding="ascii")
            manifest_path.chmod(0o600)
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(profile.to_document()),
                encoding="ascii",
            )
            profile_path.chmod(0o600)

            with (
                patch(
                    "python_udf_jit.cli._installed_versions",
                    return_value={
                        "python": "3.14.3",
                        "daft": "0.7.2",
                        "ray": "2.55.0",
                        "pyarrow": "22.0.0",
                        "lance": "7.0.0",
                    },
                ),
                patch(
                    "python_udf_jit.cli._installed_daft_compatible",
                    return_value=True,
                ),
            ):
                compatibility_output = io.StringIO()
                with contextlib.redirect_stdout(compatibility_output):
                    compatibility_status = main(
                        [
                            "compatibility",
                            "--manifest",
                            str(manifest_path),
                        ]
                    )

            benchmark_output = io.StringIO()
            with contextlib.redirect_stdout(benchmark_output):
                benchmark_status = main(
                    [
                        "benchmark",
                        "mainline",
                        "--config",
                        str(profile_path),
                    ]
                )

        compatibility = json.loads(compatibility_output.getvalue())
        benchmark = json.loads(benchmark_output.getvalue())
        self.assertEqual(compatibility_status, 0)
        self.assertEqual(compatibility["reason_code"], "compatible")
        self.assertEqual(benchmark_status, 0)
        self.assertEqual(benchmark["reason_code"], "mainline_profile_valid")
        self.assertEqual(benchmark["conclusion_scope"], "directional_only")
        self.assertFalse(benchmark["blocks_functional_completion"])
        self.assertEqual(benchmark["speedup"], 0.5)
