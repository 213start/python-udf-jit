from __future__ import annotations

import argparse
import os
import stat
from importlib.resources import files
from pathlib import Path
from typing import Sequence


BOOTSTRAP_FILENAME = "python-udf-jit-bootstrap.pth"


class BootstrapInstallError(RuntimeError):
    """The explicit purelib bootstrap installation contract was violated."""


def _bootstrap_payload() -> bytes:
    payload = (
        files("python_udf_jit.resources")
        .joinpath(BOOTSTRAP_FILENAME)
        .read_bytes()
    )
    if (
        payload.count(b"\n") != 1
        or not payload.endswith(b"\n")
        or b"import daft" in payload
        or b"import ray" in payload
    ):
        raise BootstrapInstallError("bootstrap_resource_invalid")
    return payload


def _validate_purelib(purelib: Path) -> None:
    try:
        metadata = purelib.lstat()
    except OSError as error:
        raise BootstrapInstallError("purelib_unavailable") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise BootstrapInstallError("purelib_symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapInstallError("purelib_not_directory")
    if metadata.st_uid != os.geteuid():
        raise BootstrapInstallError("purelib_owner")
    if stat.S_IMODE(metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise BootstrapInstallError("purelib_permissions")


def install_bootstrap(purelib: str | os.PathLike[str]) -> Path:
    """Install the packaged `.pth` into one explicitly selected purelib root."""

    root = Path(purelib)
    _validate_purelib(root)
    payload = _bootstrap_payload()
    destination = root / BOOTSTRAP_FILENAME
    if destination.is_symlink():
        raise BootstrapInstallError("destination_symlink")
    if destination.exists():
        try:
            metadata = destination.stat()
            current = destination.read_bytes()
        except OSError as error:
            raise BootstrapInstallError("destination_unreadable") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o644
        ):
            raise BootstrapInstallError("destination_permissions")
        if current != payload:
            raise BootstrapInstallError("destination_conflict")
        return destination

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o644)
    except OSError as error:
        raise BootstrapInstallError("destination_create_failed") from error
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Python UDF JIT bootstrap into purelib."
    )
    parser.add_argument("--purelib", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    print(install_bootstrap(arguments.purelib))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
