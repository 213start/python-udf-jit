from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from python_udf_jit.runtime.layout import (
    BATCH_VIEW_LAYOUT,
    SCALAR_LAYOUT_KIND,
    SCALAR_SLOT_ABI_VERSION,
    SUPPORTED_SCALAR_TYPES,
    VECTOR_LAYOUT_KIND,
    ProcessIdentity,
    ScalarSlotDescriptor,
)
from python_udf_jit.runtime.ownership import AccessMode, OwnershipKind


ACCESS_SPEC_VERSION = 1


class LayoutRejectCode(StrEnum):
    DESCRIPTOR_VERSION_MISMATCH = "descriptor_version_mismatch"
    ARROW_LAYOUT_NOT_IMPLEMENTED = "arrow_layout_not_implemented"
    BATCH_LAYOUT_NOT_IMPLEMENTED = "batch_layout_not_implemented"
    UNKNOWN_LAYOUT_KIND = "unknown_layout_kind"
    SCALAR_TYPE_UNSUPPORTED = "scalar_type_unsupported"
    CAPACITY_UNSUPPORTED = "capacity_unsupported"
    OWNERSHIP_ACCESS_MISMATCH = "ownership_access_mismatch"


@dataclass(frozen=True)
class LayoutDecision:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class AccessSpec:
    """Strict address-free request for one physical scalar access."""

    schema_version: int
    layout_kind: str
    scalar_type: str
    nullable: bool
    ownership: str
    access_mode: str
    capacity: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise ValueError("invalid access spec version")
        if (
            not isinstance(self.layout_kind, str)
            or not self.layout_kind
            or not isinstance(self.scalar_type, str)
            or not self.scalar_type
            or not isinstance(self.ownership, str)
            or not self.ownership
            or not isinstance(self.access_mode, str)
            or not self.access_mode
            or type(self.nullable) is not bool
            or type(self.capacity) is not int
        ):
            raise ValueError("invalid access spec fields")

    def to_document(self) -> dict[str, object]:
        return {
            "access_mode": self.access_mode,
            "capacity": self.capacity,
            "layout_kind": self.layout_kind,
            "nullable": self.nullable,
            "ownership": self.ownership,
            "scalar_type": self.scalar_type,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_document(cls, document: object) -> "AccessSpec":
        expected = {
            "access_mode",
            "capacity",
            "layout_kind",
            "nullable",
            "ownership",
            "scalar_type",
            "schema_version",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid access spec document")
        return cls(
            document["schema_version"],  # type: ignore[arg-type]
            document["layout_kind"],  # type: ignore[arg-type]
            document["scalar_type"],  # type: ignore[arg-type]
            document["nullable"],  # type: ignore[arg-type]
            document["ownership"],  # type: ignore[arg-type]
            document["access_mode"],  # type: ignore[arg-type]
            document["capacity"],  # type: ignore[arg-type]
        )


def scalar_input_spec(
    scalar_type: str,
    *,
    nullable: bool,
) -> AccessSpec:
    return AccessSpec(
        ACCESS_SPEC_VERSION,
        SCALAR_LAYOUT_KIND,
        scalar_type,
        nullable,
        OwnershipKind.BORROWED_INPUT.value,
        AccessMode.READ.value,
        1,
    )


def scalar_output_spec(
    scalar_type: str,
    *,
    nullable: bool,
) -> AccessSpec:
    return AccessSpec(
        ACCESS_SPEC_VERSION,
        SCALAR_LAYOUT_KIND,
        scalar_type,
        nullable,
        OwnershipKind.OWNED_OUTPUT.value,
        AccessMode.WRITE.value,
        1,
    )


def admit_access_spec(spec: object) -> LayoutDecision:
    if not isinstance(spec, AccessSpec):
        return LayoutDecision(False, "invalid_access_spec")
    if spec.schema_version != ACCESS_SPEC_VERSION:
        return LayoutDecision(
            False,
            LayoutRejectCode.DESCRIPTOR_VERSION_MISMATCH.value,
        )
    if spec.layout_kind == VECTOR_LAYOUT_KIND:
        return LayoutDecision(
            False,
            LayoutRejectCode.ARROW_LAYOUT_NOT_IMPLEMENTED.value,
        )
    if spec.layout_kind == BATCH_VIEW_LAYOUT:
        return LayoutDecision(
            False,
            LayoutRejectCode.BATCH_LAYOUT_NOT_IMPLEMENTED.value,
        )
    if spec.layout_kind != SCALAR_LAYOUT_KIND:
        return LayoutDecision(
            False,
            LayoutRejectCode.UNKNOWN_LAYOUT_KIND.value,
        )
    if spec.scalar_type not in SUPPORTED_SCALAR_TYPES:
        return LayoutDecision(
            False,
            LayoutRejectCode.SCALAR_TYPE_UNSUPPORTED.value,
        )
    if spec.capacity != 1:
        return LayoutDecision(
            False,
            LayoutRejectCode.CAPACITY_UNSUPPORTED.value,
        )
    allowed = {
        (
            OwnershipKind.BORROWED_INPUT.value,
            AccessMode.READ.value,
        ),
        (
            OwnershipKind.OWNED_TEMPORARY.value,
            AccessMode.READ_WRITE.value,
        ),
        (
            OwnershipKind.OWNED_OUTPUT.value,
            AccessMode.WRITE.value,
        ),
    }
    if (spec.ownership, spec.access_mode) not in allowed:
        return LayoutDecision(
            False,
            LayoutRejectCode.OWNERSHIP_ACCESS_MISMATCH.value,
        )
    return LayoutDecision(True, "supported_scalar_slot")


def require_access_spec(spec: AccessSpec) -> None:
    decision = admit_access_spec(spec)
    if not decision.accepted:
        raise ValueError(decision.reason)


def descriptor_for_spec(
    spec: AccessSpec,
    *,
    epoch: str,
    access_id: str,
    descriptor_generation: int,
    process: ProcessIdentity,
) -> ScalarSlotDescriptor:
    require_access_spec(spec)
    return ScalarSlotDescriptor(
        abi_version=SCALAR_SLOT_ABI_VERSION,
        scalar_type=spec.scalar_type,
        epoch=epoch,
        access_id=access_id,
        process=process,
        layout_kind=spec.layout_kind,
        nullable=spec.nullable,
        ownership=spec.ownership,
        access_mode=spec.access_mode,
        capacity=spec.capacity,
        descriptor_generation=descriptor_generation,
    )


@dataclass(frozen=True)
class DescriptorSet:
    schema_version: int
    input_descriptor: ScalarSlotDescriptor
    output_descriptor: ScalarSlotDescriptor

    def __post_init__(self) -> None:
        if self.schema_version != ACCESS_SPEC_VERSION:
            raise ValueError("invalid descriptor set version")
        if (
            self.input_descriptor.process
            != self.output_descriptor.process
            or self.input_descriptor.epoch
            != self.output_descriptor.epoch
            or self.input_descriptor.access_id
            == self.output_descriptor.access_id
            or self.input_descriptor.ownership
            != OwnershipKind.BORROWED_INPUT.value
            or self.input_descriptor.access_mode
            != AccessMode.READ.value
            or self.output_descriptor.ownership
            != OwnershipKind.OWNED_OUTPUT.value
            or self.output_descriptor.access_mode
            != AccessMode.WRITE.value
        ):
            raise ValueError("invalid scalar descriptor set")

    def to_document(self) -> dict[str, object]:
        return {
            "input_descriptor": self.input_descriptor.to_document(),
            "output_descriptor": self.output_descriptor.to_document(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_document(cls, document: object) -> "DescriptorSet":
        expected = {
            "input_descriptor",
            "output_descriptor",
            "schema_version",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid descriptor set document")
        return cls(
            document["schema_version"],  # type: ignore[arg-type]
            ScalarSlotDescriptor.from_document(
                document["input_descriptor"]
            ),
            ScalarSlotDescriptor.from_document(
                document["output_descriptor"]
            ),
        )
