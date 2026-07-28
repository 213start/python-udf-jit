from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.core_ir import (
    LogicalType,
    SemanticCoreModule,
    SemanticOperation,
)
from python_udf_jit.compiler.region import (
    SemanticRegionGraph,
    verify_semantic_region_graph,
)
from python_udf_jit.compiler.verifier import verify_semantic_module
from python_udf_jit.runtime.descriptors import (
    AccessSpec,
    admit_access_spec,
    scalar_input_spec,
    scalar_output_spec,
)
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
    input_access_specs: tuple[AccessSpec, ...]
    output_access_spec: AccessSpec
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
            "physical_layout": {
                "inputs": [
                    spec.to_document()
                    for spec in self.input_access_specs
                ],
                "output": self.output_access_spec.to_document(),
            },
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


def _logical_type_for_scalar(scalar_type: str) -> LogicalType:
    if scalar_type == "bool":
        return LogicalType.BOOL
    if scalar_type in {"int32", "int64"}:
        return LogicalType.INT64
    if scalar_type in {"float32", "float64"}:
        return LogicalType.FLOAT64
    raise ValueError("formal artifact has an unsupported scalar type")


def _validate_physical_scalar_contract(
    semantic_core_module: SemanticCoreModule,
    input_access_specs: tuple[AccessSpec, ...],
    output_access_spec: AccessSpec,
) -> None:
    if (
        len(semantic_core_module.input_types) != 1
        or len(input_access_specs) != 1
    ):
        raise ValueError("formal artifact requires exactly one scalar input")
    for index, spec in enumerate(input_access_specs):
        expected_nullable = (
            semantic_core_module.input_nullability[index].value
            != "non_null"
        )
        if (
            spec
            != scalar_input_spec(
                spec.scalar_type,
                nullable=expected_nullable,
            )
            or _logical_type_for_scalar(spec.scalar_type)
            is not semantic_core_module.input_types[index]
        ):
            raise ValueError(
                "physical input layout does not match semantic Core IR"
            )
    expected_output_nullable = (
        semantic_core_module.output_nullability.value != "non_null"
    )
    if (
        output_access_spec
        != scalar_output_spec(
            output_access_spec.scalar_type,
            nullable=expected_output_nullable,
        )
        or _logical_type_for_scalar(output_access_spec.scalar_type)
        is not semantic_core_module.output_type
    ):
        raise ValueError(
            "physical output layout does not match semantic Core IR"
        )


def build_artifact(
    semantic_core_module: SemanticCoreModule,
    semantic_region_graph: SemanticRegionGraph,
    fallback_identity: FallbackIdentity,
    manifest: ArtifactManifest = DEFAULT_MANIFEST,
    *,
    input_access_specs: tuple[AccessSpec, ...] | None = None,
    output_access_spec: AccessSpec | None = None,
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
    resolved_inputs = (
        tuple(
            scalar_input_spec(
                value.value,
                nullable=(
                    semantic_core_module.input_nullability[index].value
                    != "non_null"
                ),
            )
            for index, value in enumerate(
                semantic_core_module.input_types
            )
        )
        if input_access_specs is None
        else input_access_specs
    )
    resolved_output = (
        scalar_output_spec(
            semantic_core_module.output_type.value,
            nullable=(
                semantic_core_module.output_nullability.value
                != "non_null"
            ),
        )
        if output_access_spec is None
        else output_access_spec
    )
    _validate_physical_scalar_contract(
        semantic_core_module,
        resolved_inputs,
        resolved_output,
    )
    guard = {
        "input_types": [
            spec.scalar_type for spec in resolved_inputs
        ],
        "output_type": resolved_output.scalar_type,
        "semantic_core_hash": semantic_core_module.semantic_hash,
        "semantic_region_hash": semantic_region_graph.semantic_hash,
        "target_python": manifest.target_python,
    }
    return PortableUdfArtifact(
        manifest,
        semantic_core_module,
        semantic_region_graph,
        resolved_inputs,
        resolved_output,
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
    layout_document = documents["physical_layout"]
    if (
        not isinstance(layout_document, dict)
        or set(layout_document) != {"inputs", "output"}
        or not isinstance(layout_document["inputs"], list)
    ):
        raise ValueError("invalid physical layout document")
    input_access_specs = tuple(
        AccessSpec.from_document(value)
        for value in layout_document["inputs"]
    )
    output_access_spec = AccessSpec.from_document(
        layout_document["output"]
    )
    for spec in (*input_access_specs, output_access_spec):
        decision = admit_access_spec(spec)
        if not decision.accepted:
            from python_udf_jit.protocol.codec import (
                ArtifactCodecError,
                ArtifactRejectCode,
            )

            raise ArtifactCodecError(
                ArtifactRejectCode.LAYOUT_UNSUPPORTED,
                decision.reason,
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
        "input_types": [
            spec.scalar_type for spec in input_access_specs
        ],
        "output_type": output_access_spec.scalar_type,
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
    _validate_physical_scalar_contract(
        semantic_module,
        input_access_specs,
        output_access_spec,
    )
    return PortableUdfArtifact(
        manifest,
        semantic_module,
        semantic_graph,
        input_access_specs,
        output_access_spec,
        guard,
        fallback,
    )
