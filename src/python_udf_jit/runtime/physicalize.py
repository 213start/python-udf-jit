from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from python_udf_jit.runtime.descriptors import (
    ACCESS_SPEC_VERSION,
    AccessSpec,
    DescriptorSet,
    descriptor_for_spec,
    require_access_spec,
)
from python_udf_jit.runtime.guards import guard_descriptor
from python_udf_jit.runtime.layout import (
    ProcessIdentity,
    normalize_scalar_value,
    scalar_type_width,
)
from python_udf_jit.runtime.ownership import AtomicOutputPublication


class PhysicalizationRejectCode(StrEnum):
    PROCESS_MISMATCH = "process_mismatch"
    CLOSED = "physicalizer_closed"
    FRAME_CLOSED = "frame_closed"
    INPUT_NOT_READABLE = "input_not_readable"
    OUTPUT_NOT_PUBLISHED = "output_not_published"
    METRICS_UNAVAILABLE = "metrics_unavailable"


class PhysicalizationError(RuntimeError):
    def __init__(self, code: PhysicalizationRejectCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class PhysicalizationMetrics:
    copied_values: int
    materialized_values: int
    boxed_values: int
    unboxed_values: int
    copied_bytes: int
    elapsed_ns: int


class ScalarCallFrame:
    """One process-local scalar input/output lifetime."""

    def __init__(
        self,
        *,
        descriptor_set: DescriptorSet,
        input_value: object,
        input_keepalive: object,
        output_validator: Callable[[object], object],
        copied_bytes: int,
        release: Callable[[], None],
    ) -> None:
        self.descriptor_set = descriptor_set
        self._input_value = input_value
        self._input_keepalive = input_keepalive
        self._publication = AtomicOutputPublication(output_validator)
        self._release = release
        self._closed = False
        self._start_ns = time.perf_counter_ns()
        self._copied_bytes = copied_bytes
        self._materialized_values = 1
        self._boxed_values = 0
        self._unboxed_values = 0
        self._metrics: PhysicalizationMetrics | None = None

    def __enter__(self) -> "ScalarCallFrame":
        return self

    def __getstate__(self) -> object:
        raise TypeError("scalar call frame is process-local")

    def _ensure_open(self) -> None:
        if self._closed:
            raise PhysicalizationError(
                PhysicalizationRejectCode.FRAME_CLOSED
            )

    def load_input(self) -> object:
        self._ensure_open()
        descriptor = self.descriptor_set.input_descriptor
        guard_descriptor(
            descriptor,
            expected_epoch=descriptor.epoch,
            expected_access_id=descriptor.access_id,
            expected_process=descriptor.process,
            expected_scalar_type=descriptor.scalar_type,
            expected_nullable=descriptor.nullable,
            expected_ownership=descriptor.ownership,
            expected_access_mode=descriptor.access_mode,
            expected_capacity=descriptor.capacity,
            expected_descriptor_generation=(
                descriptor.descriptor_generation
            ),
        )
        self._unboxed_values += int(self._input_value is not None)
        return self._input_value

    def stage_output(self, value: object) -> None:
        self._ensure_open()
        descriptor = self.descriptor_set.output_descriptor
        guard_descriptor(
            descriptor,
            expected_epoch=descriptor.epoch,
            expected_access_id=descriptor.access_id,
            expected_process=descriptor.process,
            expected_scalar_type=descriptor.scalar_type,
            expected_nullable=descriptor.nullable,
            expected_ownership=descriptor.ownership,
            expected_access_mode=descriptor.access_mode,
            expected_capacity=descriptor.capacity,
            expected_descriptor_generation=(
                descriptor.descriptor_generation
            ),
        )
        self._publication.stage(value)
        self._boxed_values += int(value is not None)

    def publish_output(self) -> object:
        self._ensure_open()
        return self._publication.publish()

    def close(self) -> None:
        if self._closed:
            return
        if not self._publication.published:
            self._publication.abort()
        self._metrics = PhysicalizationMetrics(
            copied_values=1,
            materialized_values=self._materialized_values,
            boxed_values=self._boxed_values,
            unboxed_values=self._unboxed_values,
            copied_bytes=self._copied_bytes,
            elapsed_ns=max(0, time.perf_counter_ns() - self._start_ns),
        )
        self._input_value = None
        self._input_keepalive = None
        self._closed = True
        self._release()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
        return None

    @property
    def metrics(self) -> PhysicalizationMetrics:
        if self._metrics is None:
            raise PhysicalizationError(
                PhysicalizationRejectCode.METRICS_UNAVAILABLE
            )
        return self._metrics


class ScalarPhysicalizer:
    """Thread-safe call-frame allocator for the scalar-only data plane."""

    def __init__(
        self,
        *,
        epoch: str,
        process: ProcessIdentity | None = None,
    ) -> None:
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("physicalizer epoch must be non-empty")
        self._epoch = epoch
        self._process = process or ProcessIdentity(
            os.getpid(),
            secrets.token_hex(16),
        )
        if self._process.pid != os.getpid():
            raise PhysicalizationError(
                PhysicalizationRejectCode.PROCESS_MISMATCH
            )
        self._lock = threading.RLock()
        self._next_generation = 0
        self._active_access_ids: set[str] = set()
        self._closed = False

    def __getstate__(self) -> object:
        raise TypeError("scalar physicalizer is process-local")

    @property
    def process_identity(self) -> ProcessIdentity:
        return self._process

    @property
    def active_frame_count(self) -> int:
        with self._lock:
            return len(self._active_access_ids) // 2

    def open_call(
        self,
        input_spec: AccessSpec,
        output_spec: AccessSpec,
        value: object,
        *,
        keepalive: object | None = None,
    ) -> ScalarCallFrame:
        require_access_spec(input_spec)
        require_access_spec(output_spec)
        with self._lock:
            if self._closed:
                raise PhysicalizationError(
                    PhysicalizationRejectCode.CLOSED
                )
            if self._process.pid != os.getpid():
                raise PhysicalizationError(
                    PhysicalizationRejectCode.PROCESS_MISMATCH
                )
            self._next_generation += 1
            generation = self._next_generation
            prefix = secrets.token_hex(16)
            input_id = f"{prefix}:input"
            output_id = f"{prefix}:output"
            self._active_access_ids.update((input_id, output_id))
        try:
            normalized_input = normalize_scalar_value(
                value,
                input_spec.scalar_type,
                nullable=input_spec.nullable,
            )
            input_descriptor = descriptor_for_spec(
                input_spec,
                epoch=self._epoch,
                access_id=input_id,
                descriptor_generation=generation,
                process=self._process,
            )
            output_descriptor = descriptor_for_spec(
                output_spec,
                epoch=self._epoch,
                access_id=output_id,
                descriptor_generation=generation,
                process=self._process,
            )
            descriptor_set = DescriptorSet(
                ACCESS_SPEC_VERSION,
                input_descriptor,
                output_descriptor,
            )
        except BaseException:
            with self._lock:
                self._active_access_ids.discard(input_id)
                self._active_access_ids.discard(output_id)
            raise

        def release() -> None:
            with self._lock:
                self._active_access_ids.discard(input_id)
                self._active_access_ids.discard(output_id)

        return ScalarCallFrame(
            descriptor_set=descriptor_set,
            input_value=normalized_input,
            input_keepalive=value if keepalive is None else keepalive,
            output_validator=lambda output: normalize_scalar_value(
                output,
                output_spec.scalar_type,
                nullable=output_spec.nullable,
            ),
            copied_bytes=(
                0
                if normalized_input is None
                else scalar_type_width(input_spec.scalar_type)
            ),
            release=release,
        )

    def close(self) -> None:
        with self._lock:
            if self._active_access_ids:
                raise RuntimeError(
                    "cannot close physicalizer with active scalar frames"
                )
            self._closed = True
