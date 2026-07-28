from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_RUNTIME_TOTALS = (1177, 66, 130)
EXPECTED_UDF_RUNTIME_CASES = (
    "UdfDataIntrinsicTest.RuntimeHelpersEnforceBorrowAndLifetime",
    "UdfDataIntrinsicTest.RuntimeHelpersRejectCrossProcessCapsule",
    "UdfDataIntrinsicTest.ExactGuardedLoadProducesPrimitiveHIR",
    "UdfDataIntrinsicTest.HIRMetadataMatchesPrimitiveRead",
    "UdfDataIntrinsicTest.LIRCallsFloat64SlotLoadHelper",
    "UdfDataIntrinsicHIRTest.ParserPrinterAndOutputTypePreserveGuardedPrimitiveLoad",
)


class CinderXEvidenceError(ValueError):
    """A CinderX test artifact cannot support the formal acceptance claim."""


def _private_file(path: Path, field: str) -> bytes:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise CinderXEvidenceError(f"{field}_unreadable") from error
    if not path.is_file() or mode != 0o600:
        raise CinderXEvidenceError(f"{field}_mode_invalid")
    try:
        return path.read_bytes()
    except OSError as error:
        raise CinderXEvidenceError(f"{field}_unreadable") from error


def _text(path: Path, field: str) -> tuple[str, str]:
    payload = _private_file(path, field)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CinderXEvidenceError(f"{field}_encoding_invalid") from error
    return text, hashlib.sha256(payload).hexdigest()


def _document(path: Path, field: str) -> tuple[Mapping[str, Any], str]:
    text, digest = _text(path, field)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise CinderXEvidenceError(f"{field}_json_invalid") from error
    if not isinstance(document, Mapping):
        raise CinderXEvidenceError(f"{field}_shape_invalid")
    return document, digest


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CinderXEvidenceError(f"{field}_invalid")
    return value


def _proof_digest(
    *,
    identity: Mapping[str, Any],
    runtime_tests: Mapping[str, Any],
    python_tests: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> str:
    proof_material = json.dumps(
        {
            "identity": identity,
            "runtime_tests": runtime_tests,
            "python_tests": python_tests,
            "artifacts": artifacts,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(proof_material).hexdigest()


def _fingerprint(document: Mapping[str, Any]) -> dict[str, object]:
    if document.get("schema_version") != 1:
        raise CinderXEvidenceError("fingerprint_schema_invalid")
    image_digest = document.get("image_digest")
    soabi = document.get("soabi")
    library = document.get("python_library")
    shared_libraries = document.get("shared_libraries")
    if (
        not isinstance(image_digest, str)
        or _IMAGE_DIGEST.fullmatch(image_digest) is None
        or document.get("python_version") != "3.14.3"
        or not isinstance(soabi, str)
        or re.fullmatch(r"cpython-314-aarch64-linux-gnu", soabi) is None
        or document.get("py_enable_shared") != 0
        or library != "/opt/python314/lib/libpython3.14.a"
        or shared_libraries != []
    ):
        raise CinderXEvidenceError("fingerprint_static_python_invalid")
    return {
        "image_digest": image_digest,
        "python_version": "3.14.3",
        "soabi": soabi,
        "py_enable_shared": 0,
        "python_library": library,
    }


def _runtime_tests(log: str) -> dict[str, object]:
    totals = tuple(
        int(value)
        for value in re.findall(
            r"100% tests passed, 0 tests failed out of ([0-9]+)", log
        )
    )
    if totals != EXPECTED_RUNTIME_TOTALS:
        raise CinderXEvidenceError("runtime_test_totals_invalid")
    for case in EXPECTED_UDF_RUNTIME_CASES:
        pattern = rf"Test\s+#[0-9]+:\s+{re.escape(case)}\s+\.+\s+Passed"
        if re.search(pattern, log) is None:
            raise CinderXEvidenceError(f"runtime_udf_case_missing:{case}")
    return {
        "normal": {"passed": totals[0], "failed": 0},
        "lightweight_frames_deopt": {"passed": totals[1], "failed": 0},
        "osr": {"passed": totals[2], "failed": 0},
        "udf_cases": list(EXPECTED_UDF_RUNTIME_CASES),
    }


def _libtest_summary(
    document: Mapping[str, Any],
    *,
    expected_count: int,
    field: str,
) -> dict[str, object]:
    expected = {
        "mode": "frame-eval-adaptive-aware",
        "returncode": 0,
        "runner": "dispatcher",
        "requires_cinderx_frame_evaluator": True,
        "requires_jit_enabled": True,
        "requires_adaptive_aware": True,
        "adaptive_compile_after": 24,
        "test_count": expected_count,
    }
    if any(document.get(name) != value for name, value in expected.items()):
        raise CinderXEvidenceError(f"{field}_contract_invalid")
    return {
        "module_count": expected_count,
        "mode": expected["mode"],
        "runner": expected["runner"],
        "adaptive_compile_after": expected["adaptive_compile_after"],
        "returncode": 0,
    }


def _python_tests(
    *,
    release_log: str,
    adaptive_document: Mapping[str, Any],
    adaptive_log: str,
    official_document: Mapping[str, Any],
    official_log: str,
    targeted_log: str,
) -> dict[str, object]:
    if "[       OK ] setup_release" not in release_log:
        raise CinderXEvidenceError("setup_release_missing")
    release = re.search(
        r"\[\s+OK\s+\] test_cinderx_release "
        r"\[([0-9]+) passed, ([0-9]+) skipped, ([0-9]+) deselected\]",
        release_log,
    )
    if release is None or tuple(map(int, release.groups())) != (1332, 63, 8):
        raise CinderXEvidenceError("release_pytest_totals_invalid")

    adaptive = _libtest_summary(
        adaptive_document,
        expected_count=456,
        field="adaptive_libtest",
    )
    if (
        "423 tests OK." not in adaptive_log
        or re.search(r"\n[0-9]+ tests? failed:", adaptive_log) is not None
    ):
        raise CinderXEvidenceError("adaptive_libtest_log_invalid")

    official = _libtest_summary(
        official_document,
        expected_count=26,
        field="official_skip_libtest",
    )
    if "All 26 tests OK." not in official_log:
        raise CinderXEvidenceError("official_skip_libtest_log_invalid")

    targeted = re.search(
        r"(^|\n)6 passed, 22 subtests passed in [0-9.]+s(?:\n|$)",
        targeted_log,
    )
    if targeted is None:
        raise CinderXEvidenceError("targeted_udf_python_tests_invalid")

    return {
        "release_pytest": {
            "passed": 1332,
            "failed": 0,
            "errors": 0,
            "skipped": 63,
            "deselected": 8,
        },
        "adaptive_libtest": adaptive,
        "official_skip_libtest": official,
        "udf_data_intrinsic": {
            "passed": 6,
            "subtests_passed": 22,
            "failed": 0,
        },
    }


def build_cinderx_evidence(
    *,
    cinderx_commit: str,
    source_tree_sha256: str,
    patch_sha256: str,
    cinderx_wheel_sha256: str,
    fingerprint_path: Path,
    runtime_log_path: Path,
    release_log_path: Path,
    adaptive_summary_path: Path,
    adaptive_log_path: Path,
    official_summary_path: Path,
    official_log_path: Path,
    targeted_log_path: Path,
) -> dict[str, object]:
    """Validate exact CinderX L4/L1 artifacts and return a value-free proof."""

    if _GIT_COMMIT.fullmatch(cinderx_commit) is None:
        raise CinderXEvidenceError("cinderx_commit_invalid")
    source_digest = _sha256(source_tree_sha256, "source_tree_sha256")
    patch_digest = _sha256(patch_sha256, "patch_sha256")
    wheel_digest = _sha256(
        cinderx_wheel_sha256,
        "cinderx_wheel_sha256",
    )

    fingerprint_document, fingerprint_digest = _document(
        fingerprint_path, "fingerprint"
    )
    runtime_log, runtime_digest = _text(runtime_log_path, "runtime_log")
    release_log, release_digest = _text(release_log_path, "release_log")
    adaptive_document, adaptive_summary_digest = _document(
        adaptive_summary_path, "adaptive_summary"
    )
    adaptive_log, adaptive_log_digest = _text(
        adaptive_log_path, "adaptive_log"
    )
    official_document, official_summary_digest = _document(
        official_summary_path, "official_summary"
    )
    official_log, official_log_digest = _text(
        official_log_path, "official_log"
    )
    targeted_log, targeted_log_digest = _text(
        targeted_log_path, "targeted_log"
    )

    fingerprint = _fingerprint(fingerprint_document)
    runtime_tests = _runtime_tests(runtime_log)
    python_tests = _python_tests(
        release_log=release_log,
        adaptive_document=adaptive_document,
        adaptive_log=adaptive_log,
        official_document=official_document,
        official_log=official_log,
        targeted_log=targeted_log,
    )
    artifacts = {
        "fingerprint": fingerprint_digest,
        "runtime_log": runtime_digest,
        "release_log": release_digest,
        "adaptive_summary": adaptive_summary_digest,
        "adaptive_log": adaptive_log_digest,
        "official_summary": official_summary_digest,
        "official_log": official_log_digest,
        "targeted_log": targeted_log_digest,
    }
    identity = {
        "cinderx_commit": cinderx_commit,
        "source_tree_sha256": source_digest,
        "patch_sha256": patch_digest,
        "cinderx_wheel_sha256": wheel_digest,
        **fingerprint,
    }
    return {
        "schema_version": 1,
        "status": "pass",
        "proof_sha256": _proof_digest(
            identity=identity,
            runtime_tests=runtime_tests,
            python_tests=python_tests,
            artifacts=artifacts,
        ),
        "identity": identity,
        "runtime_tests": runtime_tests,
        "python_tests": python_tests,
        "artifacts": artifacts,
    }


def validate_cinderx_evidence(proof: object) -> str:
    """Return pass/fail/incomplete for a retained CinderX proof."""

    if not isinstance(proof, Mapping):
        return "incomplete"
    required = (
        "schema_version",
        "status",
        "proof_sha256",
        "identity",
        "runtime_tests",
        "python_tests",
        "artifacts",
    )
    if any(field not in proof for field in required):
        return "incomplete"
    identity = proof["identity"]
    runtime = proof["runtime_tests"]
    python_tests = proof["python_tests"]
    artifacts = proof["artifacts"]
    if not all(
        isinstance(value, Mapping)
        for value in (identity, runtime, python_tests, artifacts)
    ):
        return "fail"

    runtime_totals = {
        "normal": 1177,
        "lightweight_frames_deopt": 66,
        "osr": 130,
    }
    runtime_valid = all(
        isinstance(runtime.get(name), Mapping)
        and runtime[name].get("passed") == count
        and runtime[name].get("failed") == 0
        for name, count in runtime_totals.items()
    )
    release = python_tests.get("release_pytest")
    adaptive = python_tests.get("adaptive_libtest")
    official = python_tests.get("official_skip_libtest")
    targeted = python_tests.get("udf_data_intrinsic")
    python_valid = (
        isinstance(release, Mapping)
        and release.get("passed") == 1332
        and release.get("failed") == 0
        and release.get("errors") == 0
        and release.get("skipped") == 63
        and release.get("deselected") == 8
        and isinstance(adaptive, Mapping)
        and adaptive.get("module_count") == 456
        and adaptive.get("mode") == "frame-eval-adaptive-aware"
        and adaptive.get("runner") == "dispatcher"
        and adaptive.get("adaptive_compile_after") == 24
        and adaptive.get("returncode") == 0
        and isinstance(official, Mapping)
        and official.get("module_count") == 26
        and official.get("mode") == "frame-eval-adaptive-aware"
        and official.get("runner") == "dispatcher"
        and official.get("adaptive_compile_after") == 24
        and official.get("returncode") == 0
        and isinstance(targeted, Mapping)
        and targeted.get("passed") == 6
        and targeted.get("subtests_passed") == 22
        and targeted.get("failed") == 0
    )
    expected_artifacts = {
        "fingerprint",
        "runtime_log",
        "release_log",
        "adaptive_summary",
        "adaptive_log",
        "official_summary",
        "official_log",
        "targeted_log",
    }
    identity_valid = (
        isinstance(identity.get("cinderx_commit"), str)
        and _GIT_COMMIT.fullmatch(str(identity["cinderx_commit"])) is not None
        and isinstance(identity.get("source_tree_sha256"), str)
        and _SHA256.fullmatch(str(identity["source_tree_sha256"])) is not None
        and isinstance(identity.get("patch_sha256"), str)
        and _SHA256.fullmatch(str(identity["patch_sha256"])) is not None
        and isinstance(identity.get("cinderx_wheel_sha256"), str)
        and _SHA256.fullmatch(str(identity["cinderx_wheel_sha256"]))
        is not None
        and isinstance(identity.get("image_digest"), str)
        and _IMAGE_DIGEST.fullmatch(str(identity["image_digest"])) is not None
        and identity.get("python_version") == "3.14.3"
        and identity.get("soabi") == "cpython-314-aarch64-linux-gnu"
        and identity.get("py_enable_shared") == 0
        and identity.get("python_library")
        == "/opt/python314/lib/libpython3.14.a"
    )
    proof_sha256 = proof["proof_sha256"]
    valid = (
        proof["schema_version"] == 1
        and proof["status"] == "pass"
        and identity_valid
        and runtime_valid
        and runtime.get("udf_cases") == list(EXPECTED_UDF_RUNTIME_CASES)
        and python_valid
        and set(artifacts) == expected_artifacts
        and all(
            isinstance(value, str) and _SHA256.fullmatch(value) is not None
            for value in artifacts.values()
        )
        and isinstance(proof_sha256, str)
        and _SHA256.fullmatch(proof_sha256) is not None
        and proof_sha256
        == _proof_digest(
            identity=identity,
            runtime_tests=runtime,
            python_tests=python_tests,
            artifacts=artifacts,
        )
    )
    return "pass" if valid else "fail"
