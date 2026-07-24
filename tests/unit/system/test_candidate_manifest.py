from __future__ import annotations

import json
import unittest
from unittest import mock

from tests.e2e.capture_candidate_manifest import capture


COMMIT = "a" * 40
CINDERX_COMMIT = "b" * 40
CINDERX_TREE = "c" * 64
CINDERX_PATCH = "d" * 64
CINDERX_WHEEL = "e" * 64
CINDERX_BASE = f"sha256:{'f' * 64}"
UDF_WHEEL = "1" * 64
CANDIDATE_IMAGE = f"sha256:{'2' * 64}"
MANIFEST = "3" * 64


def _labels() -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": COMMIT,
        "org.python-udf-jit.cinderx-commit": CINDERX_COMMIT,
        "org.python-udf-jit.cinderx-source-tree-sha256": CINDERX_TREE,
        "org.python-udf-jit.cinderx-patch-sha256": CINDERX_PATCH,
        "org.python-udf-jit.cinderx-wheel-sha256": CINDERX_WHEEL,
        "org.python-udf-jit.cinderx-base-image-digest": CINDERX_BASE,
        "org.python-udf-jit.wheel-sha256": UDF_WHEEL,
    }


def _runtime() -> dict[str, str]:
    return {
        "python_version": "3.14.3",
        "soabi": "cpython-314-aarch64-linux-gnu",
        "daft_version": "0.7.2",
        "ray_version": "2.55.0",
        "pyarrow_version": "22.0.0",
        "candidate_manifest_sha256": MANIFEST,
    }


class CandidateManifestTests(unittest.TestCase):
    def _capture(self, labels: dict[str, str]) -> dict[str, object]:
        inspect = json.dumps(
            [
                {
                    "Image": CANDIDATE_IMAGE,
                    "Config": {"Labels": labels},
                }
            ]
        )
        with (
            mock.patch(
                "tests.e2e.capture_candidate_manifest._runtime",
                side_effect=(_runtime(), _runtime(), _runtime()),
            ),
            mock.patch(
                "tests.e2e.capture_candidate_manifest._run",
                return_value=inspect,
            ),
        ):
            return capture(
                containers={
                    "ray-head-driver": "head",
                    "ray-worker-1": "worker-1",
                    "ray-worker-2": "worker-2",
                },
                source_git_commit=COMMIT,
                cinderx_commit=CINDERX_COMMIT,
                cinderx_source_tree_sha256=CINDERX_TREE,
                cinderx_patch_sha256=CINDERX_PATCH,
                cinderx_wheel_sha256=CINDERX_WHEEL,
                cinderx_base_image_digest=CINDERX_BASE,
                udf_jit_wheel_sha256=UDF_WHEEL,
            )

    def test_manifest_binds_installed_cinderx_wheel_and_base_image(self) -> None:
        document = self._capture(_labels())

        self.assertEqual(document["image_digest"], CANDIDATE_IMAGE)
        self.assertEqual(document["cinderx_wheel_sha256"], CINDERX_WHEEL)
        self.assertEqual(document["cinderx_base_image_digest"], CINDERX_BASE)

    def test_any_cinderx_label_drift_is_rejected(self) -> None:
        for label in (
            "org.python-udf-jit.cinderx-wheel-sha256",
            "org.python-udf-jit.cinderx-base-image-digest",
        ):
            with self.subTest(label=label):
                labels = _labels()
                labels[label] = "9" * 64
                with self.assertRaisesRegex(
                    RuntimeError,
                    "candidate source/image label drift",
                ):
                    self._capture(labels)


if __name__ == "__main__":
    unittest.main()
