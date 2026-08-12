from __future__ import annotations

from enum import StrEnum
from dataclasses import dataclass

from python_udf_jit.runtime.layout import (
    ARROW_BATCH_ABI_VERSION,
    FLOAT64_SCALAR_TYPE,
    SCALAR_LAYOUT_KIND,
    SCALAR_SLOT_ABI_VERSION,
    ArrowBatchDescriptor,
    ProcessIdentity,
    ScalarSlotDescriptor,
)
from python_udf_jit.runtime.ownership import AccessMode, OwnershipKind


class DescriptorRejectCode(StrEnum):
    INVALID_DESCRIPTOR = "invalid_descriptor"
    ABI_MISMATCH = "abi_mismatch"
    LAYOUT_MISMATCH = "layout_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    NULLABILITY_MISMATCH = "nullability_mismatch"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    ACCESS_MODE_MISMATCH = "access_mode_mismatch"
    CAPACITY_MISMATCH = "capacity_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    EPOCH_MISMATCH = "epoch_mismatch"
    ACCESS_MISMATCH = "access_mismatch"
    PROCESS_MISMATCH = "process_mismatch"
    BORROW_EXPIRED = "borrow_expired"


class DescriptorGuardError(ValueError):
    def __init__(self, code: DescriptorRejectCode) -> None:
        self.code = code
        super().__init__(code.value)


class OuterGuardRejectCode(StrEnum):
    INVALID_CONTEXT = "invalid_context"
    PROCESS_MISMATCH = "process_mismatch"
    ARTIFACT_MISMATCH = "artifact_mismatch"
    MANIFEST_MISMATCH = "manifest_mismatch"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    CALLABLE_MISMATCH = "callable_mismatch"
    TARGET_PYTHON_MISMATCH = "target_python_mismatch"
    TARGET_SOABI_MISMATCH = "target_soabi_mismatch"
    CPU_FEATURE_MISMATCH = "cpu_feature_mismatch"
    VARIANT_MISMATCH = "variant_mismatch"
    INPUT_SHAPE_MISMATCH = "input_shape_mismatch"
    INPUT_TYPE_MISMATCH = "input_type_mismatch"


class OuterGuardError(ValueError):
    def __init__(self, code: OuterGuardRejectCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class OuterGuardExpectation:
    """Immutable, address-free assumptions checked before machine-code entry."""

    artifact_content_sha256: str
    experiment_manifest_sha256: str
    semantic_hash: str
    schema_fingerprint: str
    callable_code_sha256: str
    target_python: str
    target_soabi_prefix: str
    cpu_features: tuple[str, ...]


@dataclass(frozen=True)
class OuterGuardObservation:
    artifact_content_sha256: str
    experiment_manifest_sha256: str
    semantic_hash: str
    schema_fingerprint: str
    callable_code_sha256: str
    target_python: str
    target_soabi: str
    cpu_features: tuple[str, ...]
    process: ProcessIdentity


def guard_outer_entry(
    expectation: OuterGuardExpectation,
    observation: OuterGuardObservation,
    *,
    expected_process: ProcessIdentity,
) -> None:
    """Run all portable and target guards before a cached executable is entered."""

    if not isinstance(expectation, OuterGuardExpectation) or not isinstance(
        observation, OuterGuardObservation
    ):
        raise OuterGuardError(OuterGuardRejectCode.INVALID_CONTEXT)
    if observation.process != expected_process:
        raise OuterGuardError(OuterGuardRejectCode.PROCESS_MISMATCH)
    comparisons = (
        (
            observation.artifact_content_sha256,
            expectation.artifact_content_sha256,
            OuterGuardRejectCode.ARTIFACT_MISMATCH,
        ),
        (
            observation.experiment_manifest_sha256,
            expectation.experiment_manifest_sha256,
            OuterGuardRejectCode.MANIFEST_MISMATCH,
        ),
        (
            observation.semantic_hash,
            expectation.semantic_hash,
            OuterGuardRejectCode.SEMANTIC_MISMATCH,
        ),
        (
            observation.schema_fingerprint,
            expectation.schema_fingerprint,
            OuterGuardRejectCode.SCHEMA_MISMATCH,
        ),
        (
            observation.callable_code_sha256,
            expectation.callable_code_sha256,
            OuterGuardRejectCode.CALLABLE_MISMATCH,
        ),
        (
            observation.target_python,
            expectation.target_python,
            OuterGuardRejectCode.TARGET_PYTHON_MISMATCH,
        ),
        (
            observation.cpu_features,
            expectation.cpu_features,
            OuterGuardRejectCode.CPU_FEATURE_MISMATCH,
        ),
    )
    for actual, expected, code in comparisons:
        if actual != expected:
            raise OuterGuardError(code)
    if not observation.target_soabi.startswith(expectation.target_soabi_prefix):
        raise OuterGuardError(OuterGuardRejectCode.TARGET_SOABI_MISMATCH)


def guard_descriptor(
    descriptor: object,
    *,
    expected_epoch: str,
    expected_access_id: str,
    expected_process: ProcessIdentity,
    expected_abi_version: int = SCALAR_SLOT_ABI_VERSION,
    expected_layout_kind: str = SCALAR_LAYOUT_KIND,
    expected_scalar_type: str = FLOAT64_SCALAR_TYPE,
    expected_nullable: bool = False,
    expected_ownership: str = OwnershipKind.OWNED_TEMPORARY.value,
    expected_access_mode: str = AccessMode.READ_WRITE.value,
    expected_capacity: int = 1,
    expected_descriptor_generation: int = 1,
) -> ScalarSlotDescriptor:
    """Validate every address-free descriptor dimension before a data load."""

    if not isinstance(descriptor, ScalarSlotDescriptor):
        raise DescriptorGuardError(DescriptorRejectCode.INVALID_DESCRIPTOR)
    if descriptor.abi_version != expected_abi_version:
        raise DescriptorGuardError(DescriptorRejectCode.ABI_MISMATCH)
    if descriptor.layout_kind != expected_layout_kind:
        raise DescriptorGuardError(DescriptorRejectCode.LAYOUT_MISMATCH)
    if descriptor.scalar_type != expected_scalar_type:
        raise DescriptorGuardError(DescriptorRejectCode.TYPE_MISMATCH)
    if descriptor.nullable != expected_nullable:
        raise DescriptorGuardError(
            DescriptorRejectCode.NULLABILITY_MISMATCH
        )
    if descriptor.ownership != expected_ownership:
        raise DescriptorGuardError(
            DescriptorRejectCode.OWNERSHIP_MISMATCH
        )
    if descriptor.access_mode != expected_access_mode:
        raise DescriptorGuardError(
            DescriptorRejectCode.ACCESS_MODE_MISMATCH
        )
    if descriptor.capacity != expected_capacity:
        raise DescriptorGuardError(
            DescriptorRejectCode.CAPACITY_MISMATCH
        )
    if (
        descriptor.descriptor_generation
        != expected_descriptor_generation
    ):
        raise DescriptorGuardError(
            DescriptorRejectCode.GENERATION_MISMATCH
        )
    if descriptor.epoch != expected_epoch:
        raise DescriptorGuardError(DescriptorRejectCode.EPOCH_MISMATCH)
    if descriptor.access_id != expected_access_id:
        raise DescriptorGuardError(DescriptorRejectCode.ACCESS_MISMATCH)
    if descriptor.process != expected_process:
        raise DescriptorGuardError(DescriptorRejectCode.PROCESS_MISMATCH)
    return descriptor


def guard_arrow_batch_descriptor(
    descriptor: object,
    *,
    expected_epoch: str,
    expected_borrow_id: str,
    expected_process: ProcessIdentity,
    expected_physical_type: str | None = None,
    expected_length: int | None = None,
    expected_descriptor_generation: int = 1,
) -> ArrowBatchDescriptor:
    """Validate all portable Arrow authority before the first buffer load."""

    if not isinstance(descriptor, ArrowBatchDescriptor):
        raise DescriptorGuardError(DescriptorRejectCode.INVALID_DESCRIPTOR)
    if descriptor.abi_version != ARROW_BATCH_ABI_VERSION:
        raise DescriptorGuardError(DescriptorRejectCode.ABI_MISMATCH)
    if descriptor.layout_kind != "arrow_array":
        raise DescriptorGuardError(DescriptorRejectCode.LAYOUT_MISMATCH)
    if (
        expected_physical_type is not None
        and descriptor.physical_type != expected_physical_type
    ):
        raise DescriptorGuardError(DescriptorRejectCode.TYPE_MISMATCH)
    if descriptor.ownership != OwnershipKind.BORROWED_INPUT.value:
        raise DescriptorGuardError(DescriptorRejectCode.OWNERSHIP_MISMATCH)
    if descriptor.access_mode != AccessMode.READ.value:
        raise DescriptorGuardError(DescriptorRejectCode.ACCESS_MODE_MISMATCH)
    if expected_length is not None and descriptor.length != expected_length:
        raise DescriptorGuardError(DescriptorRejectCode.CAPACITY_MISMATCH)
    if descriptor.descriptor_generation != expected_descriptor_generation:
        raise DescriptorGuardError(DescriptorRejectCode.GENERATION_MISMATCH)
    if descriptor.epoch != expected_epoch:
        raise DescriptorGuardError(DescriptorRejectCode.EPOCH_MISMATCH)
    if descriptor.borrow_id != expected_borrow_id:
        raise DescriptorGuardError(DescriptorRejectCode.ACCESS_MISMATCH)
    if descriptor.process != expected_process:
        raise DescriptorGuardError(DescriptorRejectCode.PROCESS_MISMATCH)
    return descriptor
