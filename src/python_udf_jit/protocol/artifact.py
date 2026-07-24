from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.core_ir import CoreNode, CoreUdfModule
from python_udf_jit.compiler.region import VerifiedRegion
from python_udf_jit.compiler.verifier import verify_core_module, verify_region
from python_udf_jit.protocol.manifest import DEFAULT_MANIFEST, ArtifactManifest


REQUIRED_SECTIONS = ("manifest", "core_ir", "region", "guard", "fallback")


@dataclass(frozen=True)
class PortableUdfArtifact:
    manifest: ArtifactManifest
    core_module: CoreUdfModule
    region: VerifiedRegion
    guard_template: dict[str, Any]
    fallback_identity: FallbackIdentity

    def section_documents(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_document(),
            "core_ir": self.core_module.to_document(),
            "region": self.region.to_document(),
            "guard": dict(self.guard_template),
            "fallback": self.fallback_identity.to_document(),
        }

    @property
    def content_sha256(self) -> str:
        import hashlib

        from python_udf_jit.protocol.codec import encode_artifact

        return hashlib.sha256(encode_artifact(self)).hexdigest()

    def with_core_nodes(self, nodes: tuple[CoreNode, ...]) -> "PortableUdfArtifact":
        return dataclasses.replace(self, core_module=dataclasses.replace(self.core_module, nodes=nodes))


def build_artifact(
    module: CoreUdfModule,
    region: VerifiedRegion,
    fallback_identity: FallbackIdentity,
    manifest: ArtifactManifest = DEFAULT_MANIFEST,
) -> PortableUdfArtifact:
    verify_core_module(module, max_nodes=manifest.max_nodes, max_constants=manifest.max_constants)
    verify_region(module, region)
    if fallback_identity != module.fallback_identity:
        raise ValueError("fallback identity must match the verified Core IR")
    guard = {
        "input_type": "float64",
        "output_type": "float64",
        "semantic_hash": module.semantic_hash,
        "target_python": manifest.target_python,
    }
    return PortableUdfArtifact(manifest, module, region, guard, fallback_identity)


def artifact_from_documents(
    documents: dict[str, Any], runtime_manifest: ArtifactManifest = DEFAULT_MANIFEST
) -> PortableUdfArtifact:
    if set(documents) != set(REQUIRED_SECTIONS):
        raise ValueError("artifact required sections do not match")
    manifest = ArtifactManifest.from_document(documents["manifest"])
    if not manifest.compatible_with(runtime_manifest):
        from python_udf_jit.protocol.codec import ArtifactCodecError, ArtifactRejectCode

        raise ArtifactCodecError(ArtifactRejectCode.MANIFEST_INCOMPATIBLE)
    core_document = documents["core_ir"]
    if not isinstance(core_document, dict) or not isinstance(core_document.get("nodes"), list):
        raise ValueError("invalid Core IR node document")
    node_documents = core_document["nodes"]
    if len(node_documents) > runtime_manifest.max_nodes:
        from python_udf_jit.protocol.codec import ArtifactCodecError, ArtifactRejectCode

        raise ArtifactCodecError(ArtifactRejectCode.NODE_LIMIT)
    constant_count = sum(
        1
        for node in node_documents
        if isinstance(node, dict) and node.get("op") == "const.f64"
    )
    if constant_count > runtime_manifest.max_constants:
        from python_udf_jit.protocol.codec import ArtifactCodecError, ArtifactRejectCode

        raise ArtifactCodecError(ArtifactRejectCode.CONSTANT_LIMIT)
    module = CoreUdfModule.from_document(core_document)
    region = VerifiedRegion.from_document(documents["region"])
    fallback = FallbackIdentity.from_document(documents["fallback"])
    guard = documents["guard"]
    if not isinstance(guard, dict) or set(guard) != {
        "input_type",
        "output_type",
        "semantic_hash",
        "target_python",
    }:
        raise ValueError("invalid guard template")
    if guard != {
        "input_type": "float64",
        "output_type": "float64",
        "semantic_hash": module.semantic_hash,
        "target_python": manifest.target_python,
    }:
        raise ValueError("guard template does not match artifact semantics")
    if fallback != module.fallback_identity:
        raise ValueError("fallback identity mismatch")
    verify_core_module(module, max_nodes=manifest.max_nodes, max_constants=manifest.max_constants)
    verify_region(module, region)
    return PortableUdfArtifact(manifest, module, region, guard, fallback)
