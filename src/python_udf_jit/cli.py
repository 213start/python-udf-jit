from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import stat
from pathlib import Path

from python_udf_jit.benchmarks.mainline import (
    ProfileError,
    validate_profile_document,
)
from python_udf_jit.governance.explain import decode_explain_report
from python_udf_jit.integration.daft_ray.compatibility import (
    validate_daft_compatibility,
)
from python_udf_jit.protocol.codec import decode_artifact


class CliError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _private_regular_file(
    path: Path,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> Path:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CliError("local_file_not_authorized")
    if metadata.st_size > max_bytes:
        raise CliError("local_file_too_large")
    return path


def _read_bytes(path: Path) -> bytes:
    return _private_regular_file(path).read_bytes()


def _read_json(path: Path, reason_code: str) -> tuple[object, bytes]:
    payload = _read_bytes(path)
    try:
        return json.loads(payload.decode("ascii")), payload
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CliError(reason_code) from error


def _decode_artifact(path: Path):
    try:
        return decode_artifact(_read_bytes(path))
    except CliError:
        raise
    except (TypeError, ValueError) as error:
        raise CliError("artifact_invalid") from error


def _artifact_verify(path: Path) -> dict[str, object]:
    artifact = _decode_artifact(path)
    return {
        "schema_version": 1,
        "status": "pass",
        "artifact_content_sha256": artifact.content_sha256,
        "format_major": artifact.manifest.artifact_format_major,
        "format_minor": artifact.manifest.artifact_format_minor,
        "machine_code_mapped": False,
    }


def _artifact_inspect(path: Path) -> dict[str, object]:
    artifact = _decode_artifact(path)
    return {
        "schema_version": 1,
        "status": "pass",
        "artifact_content_sha256": artifact.content_sha256,
        "artifact_manifest_sha256": artifact.manifest.sha256,
        "format_major": artifact.manifest.artifact_format_major,
        "format_minor": artifact.manifest.artifact_format_minor,
        "target_python": artifact.manifest.target_python,
        "target_soabi": artifact.manifest.target_soabi,
        "semantic_core_sha256": artifact.semantic_core_module.semantic_hash,
        "semantic_region_sha256": artifact.semantic_region_graph.semantic_hash,
        "operation_count": len(artifact.semantic_core_module.operations),
        "region_count": len(artifact.semantic_region_graph.regions),
        "machine_code_mapped": False,
    }


def _explain(path: Path) -> dict[str, object]:
    document, _payload = _read_json(path, "explain_report_invalid")
    try:
        return decode_explain_report(document)
    except (TypeError, ValueError) as error:
        raise CliError("explain_report_invalid") from error


_LOCKED_DEPENDENCIES = ("python", "daft", "ray", "pyarrow")
_NON_BLOCKING_DEPENDENCIES = ("lance",)
_REQUIRED_FINGERPRINTS = (
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
)


def _compatibility_manifest(document: object) -> dict[str, object]:
    expected = {
        "schema_version",
        "artifact_kind",
        "profile",
        "plugin_mode",
        "locked_versions",
        "non_blocking_versions",
        "required_fingerprints",
        "compatibility_policy",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise CliError("compatibility_manifest_invalid")
    locked = document["locked_versions"]
    non_blocking = document["non_blocking_versions"]
    fingerprints = document["required_fingerprints"]
    if (
        document["schema_version"] != 1
        or document["artifact_kind"]
        != "scalar-piercing-environment-contract"
        or document["profile"] != "baseline"
        or document["plugin_mode"] != "off"
        or not isinstance(locked, dict)
        or tuple(locked) != _LOCKED_DEPENDENCIES
        or not isinstance(non_blocking, dict)
        or tuple(non_blocking) != _NON_BLOCKING_DEPENDENCIES
        or not isinstance(fingerprints, list)
        or tuple(fingerprints) != _REQUIRED_FINGERPRINTS
        or document["compatibility_policy"]
        != {
            "ray_daft_mismatch": "stop",
            "local_ray_fallback_allowed": False,
        }
    ):
        raise CliError("compatibility_manifest_invalid")
    for versions in (locked, non_blocking):
        if any(
            not isinstance(version, str) or not version
            for version in versions.values()
        ):
            raise CliError("compatibility_manifest_invalid")
    return document


def _installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {
        "python": platform.python_version(),
    }
    for name in ("daft", "ray", "pyarrow", "lance"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _installed_daft_compatible() -> bool:
    try:
        daft_module = importlib.import_module("daft")
        udf_module = importlib.import_module("daft.udf.udf_v2")
        dataframe_module = importlib.import_module(
            "daft.dataframe.dataframe"
        )
    except (ImportError, AttributeError):
        return False
    report = validate_daft_compatibility(
        daft_module,
        udf_module.Func,
        dataframe_module.DataFrame,
    )
    return report.compatible


def _compatibility(path: Path) -> dict[str, object]:
    raw_document, payload = _read_json(
        path,
        "compatibility_manifest_invalid",
    )
    manifest = _compatibility_manifest(raw_document)
    installed = _installed_versions()
    locked = manifest["locked_versions"]
    mismatches = [
        name
        for name in _LOCKED_DEPENDENCIES
        if installed[name] != locked[name]
    ]
    if "daft" not in mismatches and not _installed_daft_compatible():
        mismatches.append("daft_api")
    compatible = not mismatches
    return {
        "schema_version": 1,
        "status": "pass" if compatible else "fail",
        "reason_code": (
            "compatible"
            if compatible
            else "runtime_dependency_mismatch"
        ),
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "mismatches": mismatches,
    }


def _benchmark_mainline(path: Path) -> dict[str, object]:
    document, _payload = _read_json(path, "benchmark_profile_invalid")
    try:
        validate_profile_document(document)
    except (ProfileError, TypeError, ValueError) as error:
        raise CliError("benchmark_profile_invalid") from error
    assert isinstance(document, dict)
    environment = document["environment"]
    performance = document["performance"]
    functional_pass = document["functional_status"] == "pass"
    return {
        "schema_version": 1,
        "status": "pass" if functional_pass else "fail",
        "reason_code": (
            "mainline_profile_valid"
            if functional_pass
            else "functional_correctness_failed"
        ),
        "profile": "mainline-production",
        "run_id": document["run_id"],
        "correctness_sha256": document["correctness_sha256"],
        "environment_fingerprint_sha256": environment[
            "fingerprint_sha256"
        ],
        "performance_status": performance["status"],
        "speedup": performance["speedup"],
        "conclusion_scope": performance["conclusion_scope"],
        "blocks_functional_completion": False,
    }


_DIAGNOSTIC_GROUPS = (
    "source",
    "original_bytecode",
    "operation",
    "region",
    "generated_bytecode",
    "hir",
    "lir",
    "machine",
    "symbol",
    "phase",
)


def _diagnostics(arguments: argparse.Namespace) -> dict[str, object]:
    # Query-only diagnostics stay lazy so ordinary CLI and worker startup do
    # not load provenance or hotspot projection code.
    from python_udf_jit.diagnostics.bundle import DiagnosticBundleError
    from python_udf_jit.diagnostics.report import (
        diff_diagnostic_bundles,
        hotspots_diagnostic_bundle,
        trace_diagnostic_bundle,
        validate_diagnostic_bundle,
    )

    try:
        if arguments.diagnostics_command == "validate":
            return validate_diagnostic_bundle(arguments.path)
        if arguments.diagnostics_command == "trace":
            return trace_diagnostic_bundle(
                arguments.path,
                arguments.node_id,
                direction=arguments.direction,
            )
        if arguments.diagnostics_command == "hotspots":
            return hotspots_diagnostic_bundle(
                arguments.path,
                group_by=arguments.group_by,
            )
        return diff_diagnostic_bundles(
            arguments.baseline,
            arguments.candidate,
            group_by=arguments.group_by,
        )
    except KeyError as error:
        raise CliError("diagnostic_node_not_found") from error
    except (DiagnosticBundleError, OSError, TypeError, ValueError) as error:
        raise CliError("diagnostic_bundle_invalid") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="udfjitctl")
    commands = root.add_subparsers(dest="command", required=True)
    artifact = commands.add_parser("artifact")
    artifact_commands = artifact.add_subparsers(
        dest="artifact_command",
        required=True,
    )
    inspect = artifact_commands.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    verify = artifact_commands.add_parser("verify")
    verify.add_argument("path", type=Path)
    explain = commands.add_parser("explain")
    explain.add_argument("path", type=Path)
    compatibility = commands.add_parser("compatibility")
    compatibility.add_argument("--manifest", required=True, type=Path)
    benchmark = commands.add_parser("benchmark")
    benchmark_commands = benchmark.add_subparsers(
        dest="benchmark_command",
        required=True,
    )
    mainline = benchmark_commands.add_parser("mainline")
    mainline.add_argument("--config", required=True, type=Path)
    diagnostics = commands.add_parser("diagnostics")
    diagnostic_commands = diagnostics.add_subparsers(
        dest="diagnostics_command",
        required=True,
    )
    diagnostic_validate = diagnostic_commands.add_parser("validate")
    diagnostic_validate.add_argument("path", type=Path)
    diagnostic_trace = diagnostic_commands.add_parser("trace")
    diagnostic_trace.add_argument("path", type=Path)
    diagnostic_trace.add_argument("--id", dest="node_id", required=True)
    diagnostic_trace.add_argument(
        "--direction",
        choices=("upstream", "downstream", "both"),
        default="both",
    )
    diagnostic_hotspots = diagnostic_commands.add_parser("hotspots")
    diagnostic_hotspots.add_argument("path", type=Path)
    diagnostic_hotspots.add_argument(
        "--group-by",
        choices=_DIAGNOSTIC_GROUPS,
        required=True,
    )
    diagnostic_diff = diagnostic_commands.add_parser("diff")
    diagnostic_diff.add_argument("baseline", type=Path)
    diagnostic_diff.add_argument("candidate", type=Path)
    diagnostic_diff.add_argument(
        "--group-by",
        choices=_DIAGNOSTIC_GROUPS,
        default="source",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "artifact":
            if arguments.artifact_command == "inspect":
                document = _artifact_inspect(arguments.path)
            else:
                document = _artifact_verify(arguments.path)
        elif arguments.command == "explain":
            document = _explain(arguments.path)
        elif arguments.command == "compatibility":
            document = _compatibility(arguments.manifest)
        elif arguments.command == "diagnostics":
            document = _diagnostics(arguments)
        else:
            document = _benchmark_mainline(arguments.config)
    except CliError as error:
        reason_code = error.reason_code
    except OSError:
        reason_code = "local_file_unavailable"
    except Exception:
        reason_code = "internal_failure"
    else:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return (
            0
            if document.get("status", "pass") in ("pass", "valid")
            else 2
        )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "fail",
                "reason_code": reason_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
