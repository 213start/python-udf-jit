from __future__ import annotations

import json
import hashlib
import sys
import unittest
from pathlib import Path

from python_udf_jit.integration.daft_ray.environment import (
    ContractViolation,
    DockerPreflightStatus,
    load_environment_contract,
    preflight_docker,
    validate_runtime_fingerprints,
)
from python_udf_jit.integration.daft_ray.network_preflight import (
    validate_network_plan,
)


ROOT = Path(__file__).resolve().parents[2]


class EnvironmentContractTests(unittest.TestCase):
    def test_locked_baseline_contract_is_explicit(self) -> None:
        contract = load_environment_contract(
            ROOT / "config/scalar-piercing-manifest.json"
        )

        self.assertEqual("baseline", contract.profile)
        self.assertEqual("off", contract.plugin_mode)
        self.assertEqual("3.14.3", contract.locked_versions["python"])
        self.assertEqual("0.7.2", contract.locked_versions["daft"])
        self.assertEqual("2.55.0", contract.locked_versions["ray"])
        self.assertEqual("22.0.0", contract.locked_versions["pyarrow"])
        self.assertEqual("7.0.0", contract.non_blocking_versions["lance"])
        self.assertEqual("stop", contract.ray_daft_mismatch_policy)
        self.assertIn("container_image_digest", contract.required_fingerprints)
        self.assertIn("udf_jit_wheel_sha256", contract.required_fingerprints)

    def test_missing_docker_returns_needs_bootstrap_without_ray_fallback(self) -> None:
        result = preflight_docker(executable="udfjit-docker-does-not-exist")

        self.assertEqual(DockerPreflightStatus.NEEDS_BOOTSTRAP, result.status)
        self.assertFalse(result.local_ray_fallback_allowed)
        self.assertIn("not found", result.reason)

    def test_runtime_fingerprints_must_match_all_three_roles(self) -> None:
        contract = load_environment_contract(
            ROOT / "config/scalar-piercing-manifest.json"
        )
        common = {
            "container_image_digest": "sha256:" + "1" * 64,
            "python_version": "3.14.3",
            "cinderx_commit": "a" * 40,
            "cinderx_soabi": "cpython-314-x86_64-linux-gnu",
            "daft_version": "0.7.2",
            "ray_version": "2.55.0",
            "pyarrow_version": "22.0.0",
            "udf_jit_wheel_sha256": "2" * 64,
        }
        reports = [
            {"role": role, **common}
            for role in ("ray-head-driver", "ray-worker-1", "ray-worker-2")
        ]

        validated = validate_runtime_fingerprints(contract, reports)
        self.assertEqual(tuple(sorted(common.items())), validated.blocking_fingerprint)

        reports[2] = {**reports[2], "ray_version": "2.52.1"}
        with self.assertRaisesRegex(ContractViolation, "fingerprint drift"):
            validate_runtime_fingerprints(contract, reports)

    def test_manifest_contains_no_runtime_secret_value(self) -> None:
        manifest_text = (ROOT / "config/scalar-piercing-manifest.json").read_text()
        manifest = json.loads(manifest_text)

        self.assertNotIn("ray_auth_token", manifest)
        self.assertNotIn("RAY_AUTH_TOKEN", manifest_text)
        self.assertNotIn("token_value", manifest_text)

    def test_candidate_image_requires_hashed_local_wheels(self) -> None:
        dockerfile = (
            ROOT / "docker/scalar-piercing/Dockerfile.candidate"
        ).read_text(encoding="utf-8")

        self.assertIn("ARG CINDERX_WHEEL_SHA256", dockerfile)
        self.assertIn("ARG DAFT_WHEEL_SHA256", dockerfile)
        self.assertIn("ARG PYARROW_WHEEL_SHA256", dockerfile)
        self.assertIn("ARG RAY_WHEEL_SHA256", dockerfile)
        self.assertIn("ARG UDFJIT_WHEEL_SHA256", dockerfile)
        self.assertIn("ARG SOURCE_GIT_COMMIT", dockerfile)
        self.assertIn("ARG CINDERX_COMMIT", dockerfile)
        self.assertIn("ARG CINDERX_SOURCE_TREE_SHA256", dockerfile)
        self.assertIn("ARG CINDERX_PATCH_SHA256", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertIn("org.python-udf-jit.cinderx-source-tree-sha256", dockerfile)
        self.assertIn("org.python-udf-jit.cinderx-patch-sha256", dockerfile)
        self.assertIn("candidate wheel hash mismatch", dockerfile)
        self.assertIn("--force-reinstall", dockerfile)
        self.assertIn("cinderx-*.whl", dockerfile)
        self.assertIn("daft-*.whl", dockerfile)
        self.assertIn("pyarrow-*.whl", dockerfile)
        self.assertIn("ray-*.whl", dockerfile)
        self.assertIn("python_udf_jit-*.whl", dockerfile)
        self.assertIn("COPY benchmarks benchmarks", dockerfile)
        self.assertNotIn("getdaft", dockerfile)

    def test_cinderx_runtime_overlay_is_committed_and_hash_locked(self) -> None:
        root = ROOT / "vendor/cinderx/patches"
        manifest = json.loads(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
        patch = (root / manifest["patch"]).read_bytes()
        text = patch.decode("utf-8")
        changed = [
            line.removeprefix("--- a/")
            for line in text.splitlines()
            if line.startswith("--- a/")
        ]

        self.assertEqual(manifest["schema_version"], 1)
        self.assertRegex(manifest["upstream_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            hashlib.sha256(patch).hexdigest(),
            manifest["patch_sha256"],
        )
        self.assertEqual(len(changed), manifest["changed_file_count"])
        self.assertEqual(len(set(changed)), len(changed))
        self.assertTrue(
            all(path.startswith("cinderx/") for path in changed)
        )
        self.assertIn(
            "cinderx/RuntimeTests/udf_data_intrinsic_test.cpp",
            changed,
        )
        self.assertIn(
            "cinderx/PythonLib/test_cinderx/test_udf_data_intrinsic.py",
            changed,
        )

    def test_compose_network_plan_does_not_overlap_blue_98_routes(self) -> None:
        report = validate_network_plan(
            requested_subnets={
                "scalar-piercing": "172.23.240.0/24",
                "dashboard-loopback": "172.23.241.0/24",
            },
            host_routes=("172.17.0.0/16", "192.168.40.0/21"),
            docker_networks={"bridge": ("172.17.0.0/16",)},
        )

        self.assertEqual(
            (
                ("dashboard-loopback", "172.23.241.0/24"),
                ("scalar-piercing", "172.23.240.0/24"),
            ),
            report.requested_subnets,
        )

    def test_compose_network_plan_rejects_host_route_overlap(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "host route"):
            validate_network_plan(
                requested_subnets={
                    "scalar-piercing": "192.168.40.0/24",
                    "dashboard-loopback": "172.23.241.0/24",
                },
                host_routes=("192.168.40.0/21",),
                docker_networks={},
            )

    def test_compose_network_plan_rejects_existing_docker_overlap(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "Docker network"):
            validate_network_plan(
                requested_subnets={
                    "scalar-piercing": "172.23.240.0/24",
                    "dashboard-loopback": "172.23.241.0/24",
                },
                host_routes=(),
                docker_networks={"occupied": ("172.23.240.128/25",)},
            )

    def test_compose_network_plan_rejects_requested_subnet_overlap(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "overlap each other"):
            validate_network_plan(
                requested_subnets={
                    "scalar-piercing": "172.23.240.0/24",
                    "dashboard-loopback": "172.23.240.128/25",
                },
                host_routes=(),
                docker_networks={},
            )


if __name__ == "__main__":
    unittest.main()
