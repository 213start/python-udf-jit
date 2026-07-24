from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.core_ir import CoreUdfModule
from python_udf_jit.compiler.verifier import verify_core_module, verify_region


@dataclass(frozen=True)
class VerifiedRegion:
    format_version: int
    region_id: str
    entry_values: tuple[str, ...]
    exit_values: tuple[str, ...]
    operation_indexes: tuple[int, ...]
    pure: bool
    single_entry: bool
    single_exit: bool
    semantic_hash: str

    def to_document(self) -> dict[str, Any]:
        return {
            "entry_values": list(self.entry_values),
            "exit_values": list(self.exit_values),
            "format_version": self.format_version,
            "operation_indexes": list(self.operation_indexes),
            "pure": self.pure,
            "region_id": self.region_id,
            "semantic_hash": self.semantic_hash,
            "single_entry": self.single_entry,
            "single_exit": self.single_exit,
        }

    @classmethod
    def from_document(cls, document: object) -> "VerifiedRegion":
        expected = {
            "entry_values",
            "exit_values",
            "format_version",
            "operation_indexes",
            "pure",
            "region_id",
            "semantic_hash",
            "single_entry",
            "single_exit",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid region fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid region format version")
        entry = document["entry_values"]
        exits = document["exit_values"]
        indexes = document["operation_indexes"]
        if (
            not isinstance(entry, list)
            or not all(isinstance(value, str) for value in entry)
            or not isinstance(exits, list)
            or not all(isinstance(value, str) for value in exits)
            or not isinstance(indexes, list)
            or not all(type(value) is int for value in indexes)
        ):
            raise ValueError("invalid region sequences")
        if not isinstance(document["region_id"], str) or not isinstance(document["semantic_hash"], str):
            raise ValueError("invalid region strings")
        bool_values = (document["pure"], document["single_entry"], document["single_exit"])
        if not all(type(value) is bool for value in bool_values):
            raise ValueError("invalid region flags")
        return cls(
            document["format_version"],
            document["region_id"],
            tuple(entry),
            tuple(exits),
            tuple(indexes),
            *bool_values,
            document["semantic_hash"],
        )


def form_verified_region(module: CoreUdfModule) -> VerifiedRegion:
    verify_core_module(module)
    region = VerifiedRegion(
        1,
        "scalar:0",
        ("%0",),
        (module.return_value,),
        tuple(range(len(module.nodes))),
        True,
        True,
        True,
        module.semantic_hash,
    )
    verify_region(module, region)
    return region
