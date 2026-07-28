from __future__ import annotations

import hmac
import os
import secrets
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from python_udf_jit.runtime.guards import DescriptorGuardError, guard_descriptor
from python_udf_jit.runtime.layout import (
    SCALAR_SLOT_ABI_VERSION,
    ProcessIdentity,
    ScalarSlotBackend,
    ScalarSlotDescriptor,
)


class CapabilityRejectCode(StrEnum):
    PROCESS_MISMATCH = "process_mismatch"
    PROCESS_GENERATION_MISMATCH = "process_generation_mismatch"
    REGISTRY_MISMATCH = "registry_mismatch"
    UNKNOWN_ACCESS = "unknown_access"
    GENERATION_MISMATCH = "generation_mismatch"
    TOKEN_MISMATCH = "token_mismatch"
    DESCRIPTOR_MISMATCH = "descriptor_mismatch"
    NOT_BORROWED = "not_borrowed"
    BORROW_MISMATCH = "borrow_mismatch"
    ALREADY_BORROWED = "already_borrowed"
    IN_USE = "in_use"


class CapabilityError(RuntimeError):
    def __init__(self, code: CapabilityRejectCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class CapabilityHandle:
    """Serializable authority reference with no native storage address."""

    registry_id: str
    owner_pid: int
    process_generation: str
    access_id: str
    generation: int
    token: str

    def __post_init__(self) -> None:
        strings = (self.registry_id, self.process_generation, self.access_id, self.token)
        if not all(isinstance(value, str) and value for value in strings):
            raise ValueError("invalid capability handle strings")
        if type(self.owner_pid) is not int or self.owner_pid <= 0:
            raise ValueError("invalid capability owner pid")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("invalid capability generation")

    def to_document(self) -> dict[str, object]:
        return {
            "registry_id": self.registry_id,
            "owner_pid": self.owner_pid,
            "process_generation": self.process_generation,
            "access_id": self.access_id,
            "generation": self.generation,
            "token": self.token,
        }

    @classmethod
    def from_document(cls, document: object) -> "CapabilityHandle":
        expected = {
            "registry_id",
            "owner_pid",
            "process_generation",
            "access_id",
            "generation",
            "token",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid capability handle fields")
        strings = (
            document["registry_id"],
            document["process_generation"],
            document["access_id"],
            document["token"],
        )
        if not all(isinstance(value, str) and value for value in strings):
            raise ValueError("invalid capability handle strings")
        if type(document["owner_pid"]) is not int or document["owner_pid"] <= 0:
            raise ValueError("invalid capability owner pid")
        if type(document["generation"]) is not int or document["generation"] <= 0:
            raise ValueError("invalid capability generation")
        return cls(
            document["registry_id"],  # type: ignore[arg-type]
            document["owner_pid"],  # type: ignore[arg-type]
            document["process_generation"],  # type: ignore[arg-type]
            document["access_id"],  # type: ignore[arg-type]
            document["generation"],  # type: ignore[arg-type]
            document["token"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class _GuardedSlot:
    handle: CapabilityHandle
    borrow_token: str


@dataclass
class _Entry:
    descriptor: ScalarSlotDescriptor
    backend: ScalarSlotBackend
    generation: int
    token: str
    borrow_token: str | None = None
    keepalive: Any = None


class BorrowedCapability(AbstractContextManager["BorrowedCapability"]):
    def __init__(
        self,
        registry: "CapabilityRegistry",
        handle: CapabilityHandle,
        borrow_token: str,
        keepalive: Any,
    ) -> None:
        self._registry = registry
        self.handle = handle
        self._borrow_token = borrow_token
        self._keepalive = keepalive
        self._active = True

    def __enter__(self) -> "BorrowedCapability":
        return self

    def write_f64(self, value: float) -> None:
        self.write_scalar(value)

    def write_scalar(self, value: object) -> None:
        self._registry._write_scalar(
            self.handle,
            self._borrow_token,
            value,
        )

    @property
    def execution_handle(self) -> object:
        return self._registry._execution_handle(self.handle, self._borrow_token)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._active:
            try:
                self._registry._end_borrow(self.handle, self._borrow_token)
            finally:
                self._active = False
                self._keepalive = None
        return None


class CapabilityRegistry:
    """A non-serializable, process-generation-local map to scalar storage."""

    def __init__(self, *, epoch: str) -> None:
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("registry epoch must be a non-empty string")
        self._epoch = epoch
        self._registry_id = secrets.token_hex(16)
        self._identity = ProcessIdentity(os.getpid(), secrets.token_hex(16))
        self._entries: dict[str, _Entry] = {}
        self._last_generation: dict[str, int] = {}
        self._active_borrows = 0
        self._lock = threading.RLock()

    @property
    def registry_id(self) -> str:
        return self._registry_id

    @property
    def process_identity(self) -> ProcessIdentity:
        return self._identity

    @property
    def epoch(self) -> str:
        return self._epoch

    def __getstate__(self) -> object:
        raise TypeError("CapabilityRegistry is process-local and cannot be serialized")

    def register(
        self,
        backend: ScalarSlotBackend,
        *,
        access_id: str | None = None,
    ) -> CapabilityHandle:
        with self._lock:
            self._assert_current_process()
            if self._active_borrows:
                raise CapabilityError(CapabilityRejectCode.IN_USE)
            if not isinstance(backend, ScalarSlotBackend):
                raise TypeError(
                    "backend must implement ScalarSlotBackend"
                )
            if access_id is None:
                access_id = secrets.token_hex(16)
            if not isinstance(access_id, str) or not access_id:
                raise ValueError(
                    "access id must be a non-empty string"
                )
            if access_id in self._entries:
                raise ValueError("access id is already registered")
            generation = self._last_generation.get(access_id, 0) + 1
            token = secrets.token_hex(32)
            descriptor = ScalarSlotDescriptor(
                abi_version=SCALAR_SLOT_ABI_VERSION,
                scalar_type=backend.scalar_type,
                epoch=self._epoch,
                access_id=access_id,
                process=self._identity,
                nullable=backend.nullable,
                descriptor_generation=generation,
            )
            self._entries[access_id] = _Entry(
                descriptor,
                backend,
                generation,
                token,
            )
            self._last_generation[access_id] = generation
            return CapabilityHandle(
                self._registry_id,
                self._identity.pid,
                self._identity.generation,
                access_id,
                generation,
                token,
            )

    def descriptor(self, handle: CapabilityHandle) -> ScalarSlotDescriptor:
        with self._lock:
            return self._resolve(handle).descriptor

    def borrow(self, handle: CapabilityHandle) -> BorrowedCapability:
        with self._lock:
            entry = self._resolve(handle)
            if entry.borrow_token is not None:
                raise CapabilityError(
                    CapabilityRejectCode.ALREADY_BORROWED
                )
            borrow_token = secrets.token_hex(32)
            entry.borrow_token = borrow_token
            try:
                keepalive = entry.backend.begin_borrow()
            except BaseException:
                entry.borrow_token = None
                raise
            entry.keepalive = keepalive
            self._active_borrows += 1
            return BorrowedCapability(
                self,
                handle,
                borrow_token,
                keepalive,
            )

    def guard_data_handle(self, handle: CapabilityHandle) -> _GuardedSlot:
        with self._lock:
            entry = self._resolve(handle)
            if entry.borrow_token is None:
                raise CapabilityError(
                    CapabilityRejectCode.NOT_BORROWED
                )
            return _GuardedSlot(handle, entry.borrow_token)

    def data_load_f64(self, guarded: object) -> float:
        value = self.data_load_scalar(guarded)
        if type(value) is not float:
            raise TypeError("float64 capability returned a non-float")
        return value

    def data_load_scalar(self, guarded: object) -> object:
        with self._lock:
            entry = self._resolve_guarded(guarded)
            return entry.backend.load_scalar(
                scalar_type=entry.descriptor.scalar_type,
                nullable=entry.descriptor.nullable,
            )

    def data_is_null(self, guarded: object) -> bool:
        return self.data_load_scalar(guarded) is None

    def data_store_scalar(self, guarded: object, value: object) -> object:
        with self._lock:
            entry = self._resolve_guarded(guarded)
            entry.backend.write_scalar(
                value,
                scalar_type=entry.descriptor.scalar_type,
                nullable=entry.descriptor.nullable,
            )
        return value

    def data_store_null(self, guarded: object) -> None:
        self.data_store_scalar(guarded, None)

    def release(self, handle: CapabilityHandle) -> None:
        with self._lock:
            entry = self._resolve(handle)
            if self._active_borrows or entry.borrow_token is not None:
                raise CapabilityError(CapabilityRejectCode.IN_USE)
            del self._entries[handle.access_id]
            entry.backend.close()

    def _assert_current_process(self) -> None:
        if os.getpid() != self._identity.pid:
            raise CapabilityError(CapabilityRejectCode.PROCESS_MISMATCH)

    def _resolve(self, handle: object) -> _Entry:
        self._assert_current_process()
        if not isinstance(handle, CapabilityHandle):
            raise CapabilityError(CapabilityRejectCode.UNKNOWN_ACCESS)
        if handle.owner_pid != self._identity.pid:
            raise CapabilityError(CapabilityRejectCode.PROCESS_MISMATCH)
        if handle.registry_id != self._registry_id:
            raise CapabilityError(CapabilityRejectCode.REGISTRY_MISMATCH)
        if handle.process_generation != self._identity.generation:
            raise CapabilityError(CapabilityRejectCode.PROCESS_GENERATION_MISMATCH)
        entry = self._entries.get(handle.access_id)
        if entry is None:
            last_generation = self._last_generation.get(handle.access_id)
            if last_generation is not None and handle.generation != last_generation:
                raise CapabilityError(CapabilityRejectCode.GENERATION_MISMATCH)
            raise CapabilityError(CapabilityRejectCode.UNKNOWN_ACCESS)
        if handle.generation != entry.generation:
            raise CapabilityError(CapabilityRejectCode.GENERATION_MISMATCH)
        if not hmac.compare_digest(handle.token, entry.token):
            raise CapabilityError(CapabilityRejectCode.TOKEN_MISMATCH)
        try:
            guard_descriptor(
                entry.descriptor,
                expected_epoch=self._epoch,
                expected_access_id=handle.access_id,
                expected_process=self._identity,
                expected_scalar_type=entry.descriptor.scalar_type,
                expected_nullable=entry.descriptor.nullable,
                expected_ownership=entry.descriptor.ownership,
                expected_access_mode=entry.descriptor.access_mode,
                expected_capacity=entry.descriptor.capacity,
                expected_descriptor_generation=handle.generation,
            )
        except DescriptorGuardError as error:
            raise CapabilityError(CapabilityRejectCode.DESCRIPTOR_MISMATCH) from error
        return entry

    def _resolve_guarded(self, guarded: object) -> _Entry:
        if not isinstance(guarded, _GuardedSlot):
            raise CapabilityError(
                CapabilityRejectCode.BORROW_MISMATCH
            )
        entry = self._resolve(guarded.handle)
        if entry.borrow_token is None:
            raise CapabilityError(
                CapabilityRejectCode.NOT_BORROWED
            )
        if not hmac.compare_digest(
            entry.borrow_token,
            guarded.borrow_token,
        ):
            raise CapabilityError(
                CapabilityRejectCode.BORROW_MISMATCH
            )
        return entry

    def _write_scalar(
        self,
        handle: CapabilityHandle,
        borrow_token: str,
        value: object,
    ) -> None:
        with self._lock:
            entry = self._resolve(handle)
            if entry.borrow_token is None:
                raise CapabilityError(
                    CapabilityRejectCode.NOT_BORROWED
                )
            if not hmac.compare_digest(
                entry.borrow_token,
                borrow_token,
            ):
                raise CapabilityError(
                    CapabilityRejectCode.BORROW_MISMATCH
                )
            entry.backend.write_scalar(
                value,
                scalar_type=entry.descriptor.scalar_type,
                nullable=entry.descriptor.nullable,
            )

    def _execution_handle(self, handle: CapabilityHandle, borrow_token: str) -> object:
        with self._lock:
            entry = self._resolve(handle)
            if entry.borrow_token is None:
                raise CapabilityError(
                    CapabilityRejectCode.NOT_BORROWED
                )
            if not hmac.compare_digest(
                entry.borrow_token,
                borrow_token,
            ):
                raise CapabilityError(
                    CapabilityRejectCode.BORROW_MISMATCH
                )
            return entry.backend.execution_handle()

    def _end_borrow(self, handle: CapabilityHandle, borrow_token: str) -> None:
        with self._lock:
            entry = self._resolve(handle)
            if entry.borrow_token is None:
                raise CapabilityError(
                    CapabilityRejectCode.NOT_BORROWED
                )
            if not hmac.compare_digest(
                entry.borrow_token,
                borrow_token,
            ):
                raise CapabilityError(
                    CapabilityRejectCode.BORROW_MISMATCH
                )
            try:
                entry.backend.end_borrow()
            finally:
                entry.borrow_token = None
                entry.keepalive = None
                self._active_borrows -= 1
