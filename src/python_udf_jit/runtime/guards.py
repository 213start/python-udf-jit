from __future__ import annotations

from enum import StrEnum
from dataclasses import dataclass

from python_udf_jit.runtime.layout import (
    FLOAT64_SCALAR_TYPE,
    SCALAR_SLOT_ABI_VERSION,
    ProcessIdentity,
    ScalarSlotDescriptor,
)


class DescriptorRejectCode(StrEnum):
    INVALID_DESCRIPTOR = "invalid_descriptor"
    ABI_MISMATCH = "abi_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    EPOCH_MISMATCH = "epoch_mismatch"
    ACCESS_MISMATCH = "access_mismatch"
    PROCESS_MISMATCH = "process_mismatch"


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
    expected_scalar_type: str = FLOAT64_SCALAR_TYPE,
) -> ScalarSlotDescriptor:
    """Validate every address-free descriptor dimension before a data load."""

    if not isinstance(descriptor, ScalarSlotDescriptor):
        raise DescriptorGuardError(DescriptorRejectCode.INVALID_DESCRIPTOR)
    if descriptor.abi_version != expected_abi_version:
        raise DescriptorGuardError(DescriptorRejectCode.ABI_MISMATCH)
    if descriptor.scalar_type != expected_scalar_type:
        raise DescriptorGuardError(DescriptorRejectCode.TYPE_MISMATCH)
    if descriptor.epoch != expected_epoch:
        raise DescriptorGuardError(DescriptorRejectCode.EPOCH_MISMATCH)
    if descriptor.access_id != expected_access_id:
        raise DescriptorGuardError(DescriptorRejectCode.ACCESS_MISMATCH)
    if descriptor.process != expected_process:
        raise DescriptorGuardError(DescriptorRejectCode.PROCESS_MISMATCH)
    return descriptor
