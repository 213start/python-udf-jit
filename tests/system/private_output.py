from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def open_private_output(path: Path) -> int:
    """Create one mode-0600 output without changing an existing parent."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise NotADirectoryError(path.parent)
    return os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )


def write_private_bytes(path: Path, payload: bytes) -> None:
    descriptor = open_private_output(path)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    write_private_bytes(
        path,
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii"),
    )
