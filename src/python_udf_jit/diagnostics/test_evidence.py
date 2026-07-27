from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Iterable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")


class TestEvidenceError(ValueError):
    """A test log cannot support the claimed formal gate."""


def _private_log(path: Path) -> tuple[str, str]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        payload = path.read_bytes()
    except OSError as error:
        raise TestEvidenceError("test_log_unreadable") from error
    if not path.is_file() or mode != 0o600:
        raise TestEvidenceError("test_log_mode_invalid")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TestEvidenceError("test_log_encoding_invalid") from error
    return text, hashlib.sha256(payload).hexdigest()


def _canonical_strings(
    values: Iterable[str],
    field: str,
    *,
    require_unique: bool,
) -> tuple[str, ...]:
    result = tuple(values)
    if (
        not result
        or any(not isinstance(value, str) or not value for value in result)
        or (require_unique and len(set(result)) != len(result))
    ):
        raise TestEvidenceError(f"{field}_invalid")
    return result


def build_unittest_evidence(
    *,
    gate_id: str,
    tier: str,
    run_id: str,
    cluster_epoch: str,
    source_git_commit: str,
    argv: Iterable[str],
    required_tests: Iterable[str],
    minimum_test_count: int,
    allow_skips: bool,
    log_path: Path,
    expected_test_count: int | None = None,
) -> dict[str, object]:
    """Build a receipt only when a private unittest log proves the gate."""

    if _IDENTIFIER.fullmatch(gate_id) is None:
        raise TestEvidenceError("gate_id_invalid")
    if tier not in {"unit", "integration", "system"}:
        raise TestEvidenceError("tier_invalid")
    if not run_id or not cluster_epoch:
        raise TestEvidenceError("run_identity_invalid")
    if _GIT_COMMIT.fullmatch(source_git_commit) is None:
        raise TestEvidenceError("source_git_commit_invalid")
    if (
        isinstance(minimum_test_count, bool)
        or not isinstance(minimum_test_count, int)
        or minimum_test_count < 1
    ):
        raise TestEvidenceError("minimum_test_count_invalid")
    if (
        expected_test_count is not None
        and (
            isinstance(expected_test_count, bool)
            or not isinstance(expected_test_count, int)
            or expected_test_count < minimum_test_count
        )
    ):
        raise TestEvidenceError("expected_test_count_invalid")
    command = _canonical_strings(argv, "argv", require_unique=False)
    required = _canonical_strings(
        required_tests,
        "required_tests",
        require_unique=True,
    )
    text, log_digest = _private_log(log_path)

    summaries = tuple(
        (int(count), float(duration))
        for count, duration in re.findall(
            r"^Ran ([0-9]+) tests? in ([0-9.]+)s$",
            text,
            flags=re.MULTILINE,
        )
    )
    if len(summaries) != 1:
        raise TestEvidenceError("unittest_summary_invalid")
    test_count, duration_seconds = summaries[0]
    final = re.search(
        r"^OK(?: \(skipped=([0-9]+)\))?$",
        text,
        flags=re.MULTILINE,
    )
    if final is None or "FAILED (" in text or test_count < minimum_test_count:
        raise TestEvidenceError("unittest_result_invalid")
    if (
        expected_test_count is not None
        and test_count != expected_test_count
    ):
        raise TestEvidenceError(
            "unittest_test_count_mismatch:"
            f"expected={expected_test_count}:actual={test_count}"
        )
    skipped = int(final.group(1) or 0)
    if not allow_skips and skipped != 0:
        raise TestEvidenceError("unittest_unexpected_skip")
    for test in required:
        if re.search(
            rf"^{re.escape(test)}(?=\s|$)",
            text,
            flags=re.MULTILINE,
        ) is None:
            raise TestEvidenceError(f"required_test_missing:{test}")

    command_payload = json.dumps(
        list(command),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    proof_material = json.dumps(
        {
            "gate_id": gate_id,
            "tier": tier,
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "source_git_commit": source_git_commit,
            "argv_sha256": hashlib.sha256(command_payload).hexdigest(),
            "log_sha256": log_digest,
            "required_tests": list(required),
            "test_count": test_count,
            "skipped": skipped,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return {
        "schema_version": 1,
        "status": "pass",
        "gate_id": gate_id,
        "tier": tier,
        "run_id": run_id,
        "cluster_epoch": cluster_epoch,
        "source_git_commit": source_git_commit,
        "argv": list(command),
        "argv_sha256": hashlib.sha256(command_payload).hexdigest(),
        "log_sha256": log_digest,
        "required_tests": list(required),
        "test_count": test_count,
        "skipped": skipped,
        "duration_seconds": duration_seconds,
        "proof_sha256": hashlib.sha256(proof_material).hexdigest(),
    }


def validate_unittest_evidence(
    proof: object,
    *,
    gate_id: str,
    tier: str,
    run_id: str,
    cluster_epoch: str,
    source_git_commit: str,
    required_tests: Iterable[str],
    minimum_test_count: int,
    allow_skips: bool,
    expected_test_count: int | None = None,
) -> str:
    """Return pass/fail/incomplete for a previously built unittest receipt."""

    if not isinstance(proof, dict):
        return "incomplete"
    required = tuple(required_tests)
    fields = (
        "schema_version",
        "status",
        "gate_id",
        "tier",
        "run_id",
        "cluster_epoch",
        "source_git_commit",
        "argv",
        "argv_sha256",
        "log_sha256",
        "required_tests",
        "test_count",
        "skipped",
        "duration_seconds",
        "proof_sha256",
    )
    if any(field not in proof for field in fields):
        return "incomplete"
    argv = proof["argv"]
    if not isinstance(argv, list) or not argv or not all(
        isinstance(value, str) and value for value in argv
    ):
        return "fail"
    command_payload = json.dumps(
        argv,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    proof_material = json.dumps(
        {
            "gate_id": proof["gate_id"],
            "tier": proof["tier"],
            "run_id": proof["run_id"],
            "cluster_epoch": proof["cluster_epoch"],
            "source_git_commit": proof["source_git_commit"],
            "argv_sha256": proof["argv_sha256"],
            "log_sha256": proof["log_sha256"],
            "required_tests": proof["required_tests"],
            "test_count": proof["test_count"],
            "skipped": proof["skipped"],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    valid = (
        proof["schema_version"] == 1
        and proof["status"] == "pass"
        and proof["gate_id"] == gate_id
        and proof["tier"] == tier
        and proof["run_id"] == run_id
        and proof["cluster_epoch"] == cluster_epoch
        and proof["source_git_commit"] == source_git_commit
        and proof["required_tests"] == list(required)
        and isinstance(proof["test_count"], int)
        and not isinstance(proof["test_count"], bool)
        and proof["test_count"] >= minimum_test_count
        and (
            expected_test_count is None
            or proof["test_count"] == expected_test_count
        )
        and isinstance(proof["skipped"], int)
        and not isinstance(proof["skipped"], bool)
        and proof["skipped"] >= 0
        and (allow_skips or proof["skipped"] == 0)
        and isinstance(proof["duration_seconds"], (int, float))
        and proof["duration_seconds"] >= 0
        and proof["argv_sha256"]
        == hashlib.sha256(command_payload).hexdigest()
        and all(
            isinstance(proof[field], str)
            and _SHA256.fullmatch(proof[field]) is not None
            for field in ("log_sha256", "proof_sha256")
        )
        and proof["proof_sha256"]
        == hashlib.sha256(proof_material).hexdigest()
    )
    return "pass" if valid else "fail"
