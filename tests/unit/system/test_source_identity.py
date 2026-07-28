from __future__ import annotations

import hashlib
import json
import unittest

from python_udf_jit.diagnostics.cinderx_evidence import (
    EXPECTED_UDF_RUNTIME_CASES,
)
from tests.system.capture_source_identity import source_document


COMMIT = "a" * 40
CINDERX_COMMIT = "b" * 40
TREE = "c" * 64
PATCH = "d" * 64
WHEEL = "e" * 64
IMAGE = f"sha256:{'f' * 64}"
CINDERX_WHEEL = "0" * 64
CINDERX_BASE_IMAGE = f"sha256:{'1' * 64}"


def _proof() -> dict[str, object]:
    proof: dict[str, object] = {
        "schema_version": 1,
        "status": "pass",
        "identity": {
            "cinderx_commit": CINDERX_COMMIT,
            "source_tree_sha256": TREE,
            "patch_sha256": PATCH,
            "cinderx_wheel_sha256": CINDERX_WHEEL,
            "image_digest": CINDERX_BASE_IMAGE,
            "python_version": "3.14.3",
            "soabi": "cpython-314-aarch64-linux-gnu",
            "py_enable_shared": 0,
            "python_library": "/opt/python314/lib/libpython3.14.a",
        },
        "runtime_tests": {
            "normal": {"passed": 1177, "failed": 0},
            "lightweight_frames_deopt": {"passed": 66, "failed": 0},
            "osr": {"passed": 130, "failed": 0},
            "udf_cases": list(EXPECTED_UDF_RUNTIME_CASES),
        },
        "python_tests": {
            "release_pytest": {
                "passed": 1332,
                "failed": 0,
                "errors": 0,
                "skipped": 63,
                "deselected": 8,
            },
            "adaptive_libtest": {
                "module_count": 456,
                "mode": "frame-eval-adaptive-aware",
                "runner": "dispatcher",
                "adaptive_compile_after": 24,
                "returncode": 0,
            },
            "official_skip_libtest": {
                "module_count": 26,
                "mode": "frame-eval-adaptive-aware",
                "runner": "dispatcher",
                "adaptive_compile_after": 24,
                "returncode": 0,
            },
            "udf_data_intrinsic": {
                "passed": 6,
                "subtests_passed": 22,
                "failed": 0,
            },
        },
        "artifacts": {
            name: str(index) * 64
            for index, name in enumerate(
                (
                    "fingerprint",
                    "runtime_log",
                    "release_log",
                    "adaptive_summary",
                    "adaptive_log",
                    "official_summary",
                    "official_log",
                    "targeted_log",
                ),
                start=1,
            )
        },
    }
    material = {
        name: proof[name]
        for name in (
            "identity",
            "runtime_tests",
            "python_tests",
            "artifacts",
        )
    }
    proof["proof_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    return proof


def _labels() -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": COMMIT,
        "org.python-udf-jit.cinderx-commit": CINDERX_COMMIT,
        "org.python-udf-jit.cinderx-source-tree-sha256": TREE,
        "org.python-udf-jit.cinderx-patch-sha256": PATCH,
        "org.python-udf-jit.cinderx-wheel-sha256": CINDERX_WHEEL,
        "org.python-udf-jit.cinderx-base-image-digest":
            CINDERX_BASE_IMAGE,
        "org.python-udf-jit.wheel-sha256": WHEEL,
    }


class SourceIdentityTests(unittest.TestCase):
    def test_clean_git_cinderx_patch_wheel_and_image_identity_match(self) -> None:
        document = source_document(
            git_commit=COMMIT,
            dirty=False,
            image_digest=IMAGE,
            image_labels=_labels(),
            udf_jit_wheel_sha256=WHEEL,
            cinderx_wheel_sha256=CINDERX_WHEEL,
            cinderx_base_image_digest=CINDERX_BASE_IMAGE,
            cinderx_proof=_proof(),
            patch_sha256=PATCH,
        )

        self.assertFalse(document["dirty"])
        self.assertEqual(document["cinderx_source_tree_sha256"], TREE)
        self.assertEqual(document["cinderx_patch_sha256"], PATCH)
        self.assertEqual(document["cinderx_wheel_sha256"], CINDERX_WHEEL)
        self.assertEqual(
            document["cinderx_base_image_digest"],
            CINDERX_BASE_IMAGE,
        )

    def test_dirty_source_or_any_label_drift_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not match"):
            source_document(
                git_commit=COMMIT,
                dirty=True,
                image_digest=IMAGE,
                image_labels=_labels(),
                udf_jit_wheel_sha256=WHEEL,
                cinderx_wheel_sha256=CINDERX_WHEEL,
                cinderx_base_image_digest=CINDERX_BASE_IMAGE,
                cinderx_proof=_proof(),
                patch_sha256=PATCH,
            )

        labels = _labels()
        labels["org.python-udf-jit.cinderx-patch-sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "do not match"):
            source_document(
                git_commit=COMMIT,
                dirty=False,
                image_digest=IMAGE,
                image_labels=labels,
                udf_jit_wheel_sha256=WHEEL,
                cinderx_wheel_sha256=CINDERX_WHEEL,
                cinderx_base_image_digest=CINDERX_BASE_IMAGE,
                cinderx_proof=_proof(),
                patch_sha256=PATCH,
            )

        labels = _labels()
        labels["org.python-udf-jit.cinderx-wheel-sha256"] = "9" * 64
        with self.assertRaisesRegex(ValueError, "do not match"):
            source_document(
                git_commit=COMMIT,
                dirty=False,
                image_digest=IMAGE,
                image_labels=labels,
                udf_jit_wheel_sha256=WHEEL,
                cinderx_wheel_sha256=CINDERX_WHEEL,
                cinderx_base_image_digest=CINDERX_BASE_IMAGE,
                cinderx_proof=_proof(),
                patch_sha256=PATCH,
            )


if __name__ == "__main__":
    unittest.main()
