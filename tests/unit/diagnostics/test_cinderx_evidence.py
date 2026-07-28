from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.cinderx_evidence import (
    CinderXEvidenceError,
    build_cinderx_evidence,
    validate_cinderx_evidence,
)


HEX_64 = "a" * 64
COMMIT = "b" * 40
UDF_CASES = (
    "UdfDataIntrinsicTest.RuntimeHelpersEnforceBorrowAndLifetime",
    "UdfDataIntrinsicTest.RuntimeHelpersRejectCrossProcessCapsule",
    "UdfDataIntrinsicTest.ExactGuardedLoadProducesPrimitiveHIR",
    "UdfDataIntrinsicTest.HIRMetadataMatchesPrimitiveRead",
    "UdfDataIntrinsicTest.LIRCallsFloat64SlotLoadHelper",
    "UdfDataIntrinsicHIRTest.ParserPrinterAndOutputTypePreserveGuardedPrimitiveLoad",
)


def _runtime_log() -> str:
    cases = "\n".join(
        f" 1/1 Test #1: {case} ........ Passed    0.03 sec"
        for case in UDF_CASES
    )
    return (
        f"{cases}\n"
        "100% tests passed, 0 tests failed out of 1177\n"
        "100% tests passed, 0 tests failed out of 66\n"
        "100% tests passed, 0 tests failed out of 130\n"
    )


def _libtest(count: int) -> dict[str, object]:
    return {
        "mode": "frame-eval-adaptive-aware",
        "returncode": 0,
        "runner": "dispatcher",
        "requires_cinderx_frame_evaluator": True,
        "requires_jit_enabled": True,
        "requires_adaptive_aware": True,
        "adaptive_compile_after": 24,
        "test_count": count,
    }


class CinderXEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.documents: dict[str, object] = {
            "fingerprint": {
                "schema_version": 1,
                "image_digest": f"sha256:{'c' * 64}",
                "python_version": "3.14.3",
                "soabi": "cpython-314-aarch64-linux-gnu",
                "py_enable_shared": 0,
                "python_library": "/opt/python314/lib/libpython3.14.a",
                "shared_libraries": [],
            },
            "runtime": _runtime_log(),
            "release": (
                "[       OK ] setup_release (/logs/setup_release.log)\n"
                "[       OK ] test_cinderx_release "
                "[1332 passed, 63 skipped, 8 deselected] (/logs/release.log)\n"
            ),
            "adaptive_summary": _libtest(456),
            "adaptive_log": "423 tests OK.\n",
            "official_summary": _libtest(26),
            "official_log": "All 26 tests OK.\n",
            "targeted_log": (
                "...... [100%]\n"
                "6 passed, 22 subtests passed in 0.06s\n"
            ),
        }
        self.paths = {
            name: self.root / f"{name}.{'json' if isinstance(value, dict) else 'log'}"
            for name, value in self.documents.items()
        }
        self._write_all()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_all(self) -> None:
        for name, value in self.documents.items():
            path = self.paths[name]
            if isinstance(value, dict):
                path.write_text(
                    json.dumps(value, sort_keys=True),
                    encoding="utf-8",
                )
            else:
                path.write_text(str(value), encoding="utf-8")
            os.chmod(path, 0o600)

    def _build(self) -> dict[str, object]:
        return build_cinderx_evidence(
            cinderx_commit=COMMIT,
            source_tree_sha256=HEX_64,
            patch_sha256="d" * 64,
            cinderx_wheel_sha256="e" * 64,
            fingerprint_path=self.paths["fingerprint"],
            runtime_log_path=self.paths["runtime"],
            release_log_path=self.paths["release"],
            adaptive_summary_path=self.paths["adaptive_summary"],
            adaptive_log_path=self.paths["adaptive_log"],
            official_summary_path=self.paths["official_summary"],
            official_log_path=self.paths["official_log"],
            targeted_log_path=self.paths["targeted_log"],
        )

    def test_exact_static_runtime_and_python_gates_produce_proof(self) -> None:
        proof = self._build()

        self.assertEqual(proof["status"], "pass")
        self.assertEqual(validate_cinderx_evidence(proof), "pass")
        self.assertEqual(proof["identity"]["py_enable_shared"], 0)
        self.assertEqual(
            proof["identity"]["cinderx_wheel_sha256"],
            "e" * 64,
        )
        self.assertEqual(proof["runtime_tests"]["normal"]["passed"], 1177)
        self.assertEqual(len(proof["runtime_tests"]["udf_cases"]), 6)
        self.assertEqual(
            proof["python_tests"]["release_pytest"]["passed"],
            1332,
        )
        self.assertEqual(
            proof["python_tests"]["adaptive_libtest"]["module_count"],
            456,
        )
        self.assertEqual(
            proof["python_tests"]["official_skip_libtest"]["module_count"],
            26,
        )
        self.assertEqual(
            proof["python_tests"]["udf_data_intrinsic"]["passed"],
            6,
        )
        self.assertRegex(proof["proof_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(
            all(
                len(digest) == 64
                for digest in proof["artifacts"].values()
            )
        )

    def test_shared_python_or_missing_udf_case_is_rejected(self) -> None:
        fingerprint = copy.deepcopy(self.documents["fingerprint"])
        fingerprint["py_enable_shared"] = 1
        self.documents["fingerprint"] = fingerprint
        self._write_all()
        with self.assertRaisesRegex(
            CinderXEvidenceError, "fingerprint_static_python_invalid"
        ):
            self._build()

        self.documents["fingerprint"] = {
            **fingerprint,
            "py_enable_shared": 0,
        }
        self.documents["runtime"] = _runtime_log().replace(
            UDF_CASES[-1], "MissingUdfCase"
        )
        self._write_all()
        with self.assertRaisesRegex(
            CinderXEvidenceError, "runtime_udf_case_missing"
        ):
            self._build()

    def test_failed_or_weakened_libtest_is_rejected(self) -> None:
        adaptive = copy.deepcopy(self.documents["adaptive_summary"])
        adaptive["returncode"] = 2
        self.documents["adaptive_summary"] = adaptive
        self._write_all()

        with self.assertRaisesRegex(
            CinderXEvidenceError, "adaptive_libtest_contract_invalid"
        ):
            self._build()

    def test_every_raw_artifact_must_be_mode_0600(self) -> None:
        os.chmod(self.paths["targeted_log"], 0o644)

        with self.assertRaisesRegex(
            CinderXEvidenceError, "targeted_log_mode_invalid"
        ):
            self._build()

    def test_retained_proof_tampering_is_rejected(self) -> None:
        proof = self._build()
        proof["runtime_tests"]["normal"]["passed"] = 1175

        self.assertEqual(validate_cinderx_evidence(proof), "fail")

        proof = self._build()
        proof["identity"]["cinderx_wheel_sha256"] = "f" * 64

        self.assertEqual(validate_cinderx_evidence(proof), "fail")


if __name__ == "__main__":
    unittest.main()
