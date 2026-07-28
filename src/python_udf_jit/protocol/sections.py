from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class SectionCodec(IntEnum):
    CANONICAL_JSON = 1


@dataclass(frozen=True)
class ArtifactSection:
    name: str
    codec: SectionCodec
    document: Any

    def validate_name(self, max_bytes: int) -> None:
        try:
            encoded = self.name.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("section name must be ASCII") from error
        if (
            not encoded
            or len(encoded) > max_bytes
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in self.name
            )
        ):
            raise ValueError("invalid section name")


REQUIRED_SECTIONS = (
    "manifest",
    "target",
    "physical_layout",
    "semantic_core_ir",
    "semantic_region_graph",
    "guard",
    "fallback",
)
