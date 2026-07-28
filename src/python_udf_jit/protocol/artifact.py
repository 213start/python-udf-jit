from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.core_ir import (
    SemanticCoreModule,
    SemanticOperation,
)
from python_udf_jit.compiler.region import (
    SemanticRegionGraph,
    verify_semantic_region_graph,
)
from python_udf_jit.compiler.verifier import verify_semantic_module
from python_udf_jit.protocol.manifest import (
    DEFAULT_MANIFEST,
    ArtifactManifest,
)
from python_udf_jit.protocol.sections import (
    REQUIRED_SECTIONS,
    ArtifactSection,
    SectionCodec,
)


@dataclass(frozen=True)
class PortableUdfArtifact:
    manifest: ArtifactManifest
    semantic_core_module: SemanticCoreModule
    semantic_region_graph: SemanticRegionGraph
    guard_template: dict[str, Any]
    fallback_identity: FallbackIdentity
    encoded_content_sha256: str | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def section_documents(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.portable_document(),
            "target": self.manifest.target_document(),
            "semantic_core_ir": (
                self.semantic_core_module.to_document()
            ),
            "semantic_region_graph": (
                self.semantic_region_graph.to_document()
            ),
            "guard": dict(self.guard_template),
            "fallback": self.fallback_identity.to_document(),
        }

    def sections(self) -> tuple[ArtifactSection, ...]:
        documents = self.section_documents()
        return tuple(
            ArtifactSection(
                name,
                SectionCodec.CANONICAL_JSON,
                documents[name],
            )
            for name in REQUIRED_SECTIONS
        )

    @property
    def content_sha256(self) -> str:
        from python_udf_jit.protocol.codec import encode_artifact

        if self.encoded_content_sha256 is not None:
            return self.encoded_content_sha256
        return hashlib.sha256(encode_artifact(self)).hexdigest()

    def with_semantic_operations(
        self,
        operations: tuple[SemanticOperation, ...],
    ) -> "PortableUdfArtifact":
        return dataclasses.replace(
            self,
            semantic_core_module=dataclasses.replace(
                self.semantic_core_module,
                operations=operations,
            ),
            encoded_content_sha256=None,
        )


def build_artifact(
    semantic_core_module: SemanticCoreModule,
    semantic_region_graph: SemanticRegionGraph,
    fallback_identity: FallbackIdentity,
    manifest: ArtifactManifest = DEFAULT_MANIFEST,
) -> PortableUdfArtifact:
    verify_semantic_module(
        semantic_core_module,
        max_nodes=manifest.max_nodes,
        max_constants=manifest.max_constants,
    )
    verify_semantic_region_graph(
        semantic_core_module,
        semantic_region_graph,
    )
    if (
        semantic_core_module.format_version
        != manifest.semantic_core_ir_version
        or semantic_region_graph.format_version
        != manifest.semantic_region_version
    ):
        raise ValueError(
            "semantic IR versions do not match the artifact manifest"
        )
    if semantic_core_module.function_id != fallback_identity.code_sha256:
        raise ValueError(
            "semantic Core IR must match the fallback code identity"
        )
    if (
        tuple(value.value for value in semantic_core_module.input_types)
        != ("float64",)
        or semantic_core_module.output_type.value != "float64"
    ):
        raise ValueError("formal artifact supports only the scalar path")
    guard = {
        "input_types": ["float64"],
        "output_type": "float64",
        "semantic_core_hash": semantic_core_module.semantic_hash,
        "semantic_region_hash": semantic_region_graph.semantic_hash,
        "target_python": manifest.target_python,
    }
    return PortableUdfArtifact(
        manifest,
        semantic_core_module,
        semantic_region_graph,
        guard,
        fallback_identity,
    )


def artifact_from_documents(
    documents: dict[str, Any],
    runtime_manifest: ArtifactManifest = DEFAULT_MANIFEST,
) -> PortableUdfArtifact:
    if tuple(documents) != REQUIRED_SECTIONS:
        raise ValueError("artifact sections do not match the formal contract")
    manifest = ArtifactManifest.from_section_documents(
        documents["manifest"],
        documents["target"],
    )
    if not manifest.compatible_with(runtime_manifest):
        from python_udf_jit.protocol.codec import (
            ArtifactCodecError,
            ArtifactRejectCode,
        )

        raise ArtifactCodecError(
            ArtifactRejectCode.MANIFEST_INCOMPATIBLE
        )
    semantic_document = documents["semantic_core_ir"]
    if (
        not isinstance(semantic_document, dict)
        or not isinstance(semantic_document.get("operations"), list)
        or len(semantic_document["operations"])
        > runtime_manifest.max_nodes
    ):
        raise ValueError("invalid semantic Core IR document")
    constant_count = sum(
        1
        for operation in semantic_document["operations"]
        if isinstance(operation, dict)
        and operation.get("literal") is not None
    )
    if constant_count > runtime_manifest.max_constants:
        from python_udf_jit.protocol.codec import (
            ArtifactCodecError,
            ArtifactRejectCode,
        )

        raise ArtifactCodecError(
            ArtifactRejectCode.CONSTANT_LIMIT
        )
    semantic_module = SemanticCoreModule.from_document(
        semantic_document
    )
    semantic_graph = SemanticRegionGraph.from_document(
        documents["semantic_region_graph"],
        semantic_module,
    )
    fallback = FallbackIdentity.from_document(documents["fallback"])
    guard = documents["guard"]
    expected_guard = {
        "input_types": ["float64"],
        "output_type": "float64",
        "semantic_core_hash": semantic_module.semantic_hash,
        "semantic_region_hash": semantic_graph.semantic_hash,
        "target_python": manifest.target_python,
    }
    if guard != expected_guard:
        raise ValueError("guard template does not match artifact semantics")
    verify_semantic_module(
        semantic_module,
        max_nodes=manifest.max_nodes,
        max_constants=manifest.max_constants,
    )
    verify_semantic_region_graph(semantic_module, semantic_graph)
    if semantic_module.function_id != fallback.code_sha256:
        raise ValueError("semantic Core IR fallback identity mismatch")
    return PortableUdfArtifact(
        manifest,
        semantic_module,
        semantic_graph,
        guard,
        fallback,
    )
