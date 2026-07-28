from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, order=True)
class DependencyRequirement:
    """One exact, value-free runtime dependency requirement."""

    distribution: str
    version: str

    def __post_init__(self) -> None:
        if (
            not self.distribution
            or not self.version
            or len(self.distribution.encode("utf-8")) > 256
            or len(self.version.encode("utf-8")) > 256
        ):
            raise ValueError("invalid dependency requirement value")

    def to_document(self) -> dict[str, str]:
        return {
            "distribution": self.distribution,
            "version": self.version,
        }

    @classmethod
    def from_document(cls, document: object) -> "DependencyRequirement":
        if (
            not isinstance(document, dict)
            or set(document) != {"distribution", "version"}
            or not isinstance(document["distribution"], str)
            or not isinstance(document["version"], str)
        ):
            raise ValueError("invalid dependency requirement")
        return cls(document["distribution"], document["version"])


@dataclass(frozen=True)
class ArtifactManifest:
    """The first formal portable-artifact contract.

    There is deliberately no prototype reader and no version-tolerant path.
    Driver and Worker must agree on this exact format and ABI contract before
    the Worker is admitted.
    """

    artifact_format_major: int = 1
    artifact_format_minor: int = 0
    semantic_core_ir_version: int = 2
    semantic_region_version: int = 1
    runtime_abi: int = 1
    adapter_abi: int = 1
    target_python: str = "3.14.3"
    target_soabi: str = "cpython-314"
    dependency_requirements: tuple[DependencyRequirement, ...] = ()
    max_total_bytes: int = 65536
    max_sections: int = 16
    max_section_bytes: int = 32768
    max_nodes: int = 256
    max_constants: int = 256
    max_string_bytes: int = 4096
    max_json_depth: int = 16

    def __post_init__(self) -> None:
        versions = (
            self.artifact_format_major,
            self.artifact_format_minor,
            self.semantic_core_ir_version,
            self.semantic_region_version,
            self.runtime_abi,
            self.adapter_abi,
        )
        limits = (
            self.max_total_bytes,
            self.max_sections,
            self.max_section_bytes,
            self.max_nodes,
            self.max_constants,
            self.max_string_bytes,
            self.max_json_depth,
        )
        if any(type(value) is not int or value < 0 for value in versions):
            raise ValueError(
                "manifest versions must be non-negative integers"
            )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError(
                "manifest limits must be positive integers"
            )
        if (
            not isinstance(self.target_python, str)
            or not self.target_python
            or not isinstance(self.target_soabi, str)
            or not self.target_soabi
        ):
            raise ValueError("manifest targets must be non-empty strings")
        requirements = self.dependency_requirements
        if (
            len(requirements) > 128
            or tuple(sorted(requirements)) != requirements
            or len({value.distribution for value in requirements})
            != len(requirements)
        ):
            raise ValueError(
                "dependency requirements must be unique and canonical"
            )

    def portable_document(self) -> dict[str, Any]:
        return {
            "artifact_format_major": self.artifact_format_major,
            "artifact_format_minor": self.artifact_format_minor,
            "limits": {
                "max_constants": self.max_constants,
                "max_json_depth": self.max_json_depth,
                "max_nodes": self.max_nodes,
                "max_section_bytes": self.max_section_bytes,
                "max_sections": self.max_sections,
                "max_string_bytes": self.max_string_bytes,
                "max_total_bytes": self.max_total_bytes,
            },
            "semantic_core_ir_version": self.semantic_core_ir_version,
            "semantic_region_version": self.semantic_region_version,
        }

    def target_document(self) -> dict[str, Any]:
        return {
            "adapter_abi": self.adapter_abi,
            "dependencies": [
                requirement.to_document()
                for requirement in self.dependency_requirements
            ],
            "runtime_abi": self.runtime_abi,
            "target_python": self.target_python,
            "target_soabi": self.target_soabi,
        }

    def to_document(self) -> dict[str, Any]:
        return {
            **self.portable_document(),
            **self.target_document(),
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.to_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_document(cls, document: object) -> "ArtifactManifest":
        if not isinstance(document, dict):
            raise ValueError("invalid manifest fields")
        portable_names = set(cls().portable_document())
        target_names = set(cls().target_document())
        if set(document) != portable_names | target_names:
            raise ValueError("invalid manifest fields")
        return cls.from_section_documents(
            {
                name: document[name]
                for name in portable_names
            },
            {
                name: document[name]
                for name in target_names
            },
        )

    @classmethod
    def from_section_documents(
        cls,
        portable: object,
        target: object,
    ) -> "ArtifactManifest":
        portable_expected = {
            "artifact_format_major",
            "artifact_format_minor",
            "limits",
            "semantic_core_ir_version",
            "semantic_region_version",
        }
        target_expected = {
            "adapter_abi",
            "dependencies",
            "runtime_abi",
            "target_python",
            "target_soabi",
        }
        if (
            not isinstance(portable, dict)
            or set(portable) != portable_expected
            or not isinstance(target, dict)
            or set(target) != target_expected
        ):
            raise ValueError("invalid manifest section fields")
        limits = portable["limits"]
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
        dependencies = target["dependencies"]
        if not isinstance(dependencies, list):
            raise ValueError("invalid dependency manifest")
        return cls(
            artifact_format_major=portable[
                "artifact_format_major"
            ],
            artifact_format_minor=portable[
                "artifact_format_minor"
            ],
            semantic_core_ir_version=portable[
                "semantic_core_ir_version"
            ],
            semantic_region_version=portable[
                "semantic_region_version"
            ],
            runtime_abi=target["runtime_abi"],
            adapter_abi=target["adapter_abi"],
            target_python=target["target_python"],
            target_soabi=target["target_soabi"],
            dependency_requirements=tuple(
                DependencyRequirement.from_document(value)
                for value in dependencies
            ),
            **limits,
        )

    def compatible_with(self, runtime: "ArtifactManifest") -> bool:
        return (
            self.artifact_format_major
            == runtime.artifact_format_major
            and self.artifact_format_minor
            == runtime.artifact_format_minor
            and self.semantic_core_ir_version
            == runtime.semantic_core_ir_version
            and self.semantic_region_version
            == runtime.semantic_region_version
            and self.runtime_abi == runtime.runtime_abi
            and self.adapter_abi == runtime.adapter_abi
            and self.target_python == runtime.target_python
            and self.target_soabi == runtime.target_soabi
        )


DEFAULT_MANIFEST = ArtifactManifest()
