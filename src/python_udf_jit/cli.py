from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from python_udf_jit.protocol.codec import decode_artifact


class CliError(RuntimeError):
    pass


def _private_regular_file(path: Path) -> Path:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CliError("local_artifact_not_authorized")
    return path


def _artifact_verify(path: Path) -> dict[str, object]:
    artifact = decode_artifact(_private_regular_file(path).read_bytes())
    return {
        "status": "pass",
        "artifact_content_sha256": artifact.content_sha256,
        "format_major": artifact.manifest.artifact_format_major,
        "format_minor": artifact.manifest.artifact_format_minor,
        "machine_code_mapped": False,
    }


def _explain(path: Path) -> dict[str, object]:
    document = json.loads(
        _private_regular_file(path).read_text(encoding="ascii")
    )
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("dropped_business_values") is not True
    ):
        raise CliError("explain_report_invalid")
    return document


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="udfjitctl")
    commands = root.add_subparsers(dest="command", required=True)
    artifact = commands.add_parser("artifact")
    artifact_commands = artifact.add_subparsers(
        dest="artifact_command",
        required=True,
    )
    verify = artifact_commands.add_parser("verify")
    verify.add_argument("path", type=Path)
    explain = commands.add_parser("explain")
    explain.add_argument("path", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "artifact":
            document = _artifact_verify(arguments.path)
        else:
            document = _explain(arguments.path)
    except (CliError, OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason_code": str(error),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
