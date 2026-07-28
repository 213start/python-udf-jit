from __future__ import annotations

import threading
from enum import StrEnum
from typing import Callable, Generic, TypeVar


class OwnershipKind(StrEnum):
    BORROWED_INPUT = "borrowed_input"
    OWNED_TEMPORARY = "owned_temporary"
    OWNED_OUTPUT = "owned_output"


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


class PublicationRejectCode(StrEnum):
    ALREADY_STAGED = "already_staged"
    NOT_STAGED = "not_staged"
    ALREADY_PUBLISHED = "already_published"
    ABORTED = "aborted"


class PublicationError(RuntimeError):
    def __init__(self, code: PublicationRejectCode) -> None:
        self.code = code
        super().__init__(code.value)


_UNSET = object()
T = TypeVar("T")


class AtomicOutputPublication(Generic[T]):
    """Process-local one-value transaction with no partial publication."""

    def __init__(self, validator: Callable[[object], T]) -> None:
        self._validator = validator
        self._staged: object = _UNSET
        self._published: object = _UNSET
        self._aborted = False
        self._lock = threading.RLock()

    def __getstate__(self) -> object:
        raise TypeError("output publication is process-local")

    def stage(self, value: object) -> None:
        normalized = self._validator(value)
        with self._lock:
            if self._aborted:
                raise PublicationError(PublicationRejectCode.ABORTED)
            if self._published is not _UNSET:
                raise PublicationError(
                    PublicationRejectCode.ALREADY_PUBLISHED
                )
            if self._staged is not _UNSET:
                raise PublicationError(
                    PublicationRejectCode.ALREADY_STAGED
                )
            self._staged = normalized

    def publish(self) -> T:
        with self._lock:
            if self._aborted:
                raise PublicationError(PublicationRejectCode.ABORTED)
            if self._published is not _UNSET:
                raise PublicationError(
                    PublicationRejectCode.ALREADY_PUBLISHED
                )
            if self._staged is _UNSET:
                raise PublicationError(PublicationRejectCode.NOT_STAGED)
            self._published = self._staged
            self._staged = _UNSET
            return self._published  # type: ignore[return-value]

    def abort(self) -> None:
        with self._lock:
            if self._published is not _UNSET:
                raise PublicationError(
                    PublicationRejectCode.ALREADY_PUBLISHED
                )
            self._staged = _UNSET
            self._aborted = True

    @property
    def published(self) -> bool:
        with self._lock:
            return self._published is not _UNSET
