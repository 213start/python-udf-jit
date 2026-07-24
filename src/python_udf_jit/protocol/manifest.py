from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_format_major: int = 1
    artifact_format_minor: int = 0
    core_ir_version: int = 1
    region_version: int = 1
    runtime_abi: int = 1
    adapter_abi: int = 1
    target_python: str = "3.14.3"
    target_soabi: str = "cpython-314"
    max_total_bytes: int = 65536
    max_sections: int = 16
    max_section_bytes: int = 32768
    max_nodes: int = 256
    max_constants: int = 256
    max_string_bytes: int = 4096
    max_json_depth: int = 16

    def to_document(self) -> dict[str, Any]:
        return {
            "adapter_abi": self.adapter_abi,
            "artifact_format_major": self.artifact_format_major,
            "artifact_format_minor": self.artifact_format_minor,
            "core_ir_version": self.core_ir_version,
            "limits": {
                "max_constants": self.max_constants,
                "max_json_depth": self.max_json_depth,
                "max_nodes": self.max_nodes,
                "max_section_bytes": self.max_section_bytes,
                "max_sections": self.max_sections,
                "max_string_bytes": self.max_string_bytes,
                "max_total_bytes": self.max_total_bytes,
            },
            "region_version": self.region_version,
            "runtime_abi": self.runtime_abi,
            "target_python": self.target_python,
            "target_soabi": self.target_soabi,
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.to_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_document(cls, document: object) -> "ArtifactManifest":
        expected = {
            "adapter_abi",
            "artifact_format_major",
            "artifact_format_minor",
            "core_ir_version",
            "limits",
            "region_version",
            "runtime_abi",
            "target_python",
            "target_soabi",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid manifest fields")
        limits = document["limits"]
        expected_limits = {
            "max_constants",
            "max_json_depth",
            "max_nodes",
            "max_section_bytes",
            "max_sections",
            "max_string_bytes",
            "max_total_bytes",
        }
        if not isinstance(limits, dict) or set(limits) != expected_limits:
            raise ValueError("invalid manifest limit fields")
        int_names = expected - {"limits", "target_python", "target_soabi"}
        if any(type(document[name]) is not int for name in int_names):
            raise ValueError("manifest versions must be integers")
        if any(type(limits[name]) is not int or limits[name] <= 0 for name in expected_limits):
            raise ValueError("manifest limits must be positive integers")
        if not isinstance(document["target_python"], str) or not isinstance(document["target_soabi"], str):
            raise ValueError("manifest targets must be strings")
        return cls(
            artifact_format_major=document["artifact_format_major"],
            artifact_format_minor=document["artifact_format_minor"],
            core_ir_version=document["core_ir_version"],
            region_version=document["region_version"],
            runtime_abi=document["runtime_abi"],
            adapter_abi=document["adapter_abi"],
            target_python=document["target_python"],
            target_soabi=document["target_soabi"],
            **limits,
        )

    def compatible_with(self, runtime: "ArtifactManifest") -> bool:
        return self == runtime


DEFAULT_MANIFEST = ArtifactManifest()
