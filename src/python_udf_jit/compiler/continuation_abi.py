from __future__ import annotations

import re


_RESUME_ID = re.compile(r"^v1:[0-9a-f]{64}$")


def is_resume_id(value: object) -> bool:
    """Return whether *value* is the single production continuation ID form."""

    return isinstance(value, str) and _RESUME_ID.fullmatch(value) is not None
