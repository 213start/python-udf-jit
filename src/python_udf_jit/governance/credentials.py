from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar


class CredentialError(RuntimeError):
    """A process-local credential operation violated scope or lifecycle."""


@dataclass(frozen=True)
class CredentialScope:
    job_id: str
    trust_domain: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id:
            raise CredentialError("job_scope_invalid")
        if not isinstance(self.trust_domain, str) or not self.trust_domain:
            raise CredentialError("trust_domain_invalid")


@dataclass(frozen=True)
class CredentialAdmission:
    optimized_execution_allowed: bool
    reason: str


@dataclass(frozen=True)
class CredentialDistributionSnapshot:
    """Value-free generation window received from a credential control plane."""

    job_id: str
    tenant_id: str
    key_id: str
    algorithm: str
    active_generation: int
    previous_generation: int | None
    grace_expires_at_ns: int | None
    revoked_through: int
    issued_at_ns: int
    active_expires_at_ns: int
    channel_expires_at_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id:
            raise CredentialError("distribution_job_invalid")
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise CredentialError("distribution_tenant_invalid")
        if not isinstance(self.key_id, str) or not self.key_id:
            raise CredentialError("key_id_invalid")
        if self.algorithm != "hmac-sha256":
            raise CredentialError("credential_algorithm_invalid")
        if (
            type(self.active_generation) is not int
            or self.active_generation <= 0
        ):
            raise CredentialError("active_generation_invalid")
        if (
            type(self.revoked_through) is not int
            or self.revoked_through < 0
            or self.revoked_through > self.active_generation
        ):
            raise CredentialError("revoked_through_invalid")
        if (
            type(self.channel_expires_at_ns) is not int
            or self.channel_expires_at_ns < 0
        ):
            raise CredentialError("channel_expiry_invalid")
        if (
            type(self.issued_at_ns) is not int
            or self.issued_at_ns < 0
            or type(self.active_expires_at_ns) is not int
            or self.active_expires_at_ns < self.issued_at_ns
            or self.channel_expires_at_ns < self.issued_at_ns
        ):
            raise CredentialError("credential_lifetime_invalid")
        if self.previous_generation is None:
            if self.grace_expires_at_ns is not None:
                raise CredentialError("rotation_grace_without_previous")
        elif (
            type(self.previous_generation) is not int
            or self.previous_generation <= 0
            or self.previous_generation >= self.active_generation
            or type(self.grace_expires_at_ns) is not int
            or self.grace_expires_at_ns < self.issued_at_ns
            or self.grace_expires_at_ns > self.active_expires_at_ns
        ):
            raise CredentialError("previous_generation_invalid")

    def admit(
        self,
        *,
        job_id: str,
        tenant_id: str,
        key_id: str,
        generation: int,
        now_ns: int,
    ) -> CredentialAdmission:
        if not isinstance(job_id, str) or not job_id:
            raise CredentialError("admission_job_invalid")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise CredentialError("admission_tenant_invalid")
        if not isinstance(key_id, str) or not key_id:
            raise CredentialError("admission_key_id_invalid")
        if type(generation) is not int or generation <= 0:
            raise CredentialError("admission_generation_invalid")
        if type(now_ns) is not int or now_ns < 0:
            raise CredentialError("admission_time_invalid")
        if job_id != self.job_id:
            return CredentialAdmission(False, "job_scope_mismatch")
        if tenant_id != self.tenant_id:
            return CredentialAdmission(False, "tenant_scope_mismatch")
        if key_id != self.key_id:
            return CredentialAdmission(False, "key_id_mismatch")
        if now_ns > self.channel_expires_at_ns:
            return CredentialAdmission(False, "credential_channel_expired")
        if now_ns < self.issued_at_ns:
            return CredentialAdmission(False, "credential_not_yet_valid")
        if now_ns > self.active_expires_at_ns:
            return CredentialAdmission(False, "credential_expired")
        if generation <= self.revoked_through:
            return CredentialAdmission(False, "generation_revoked")
        if generation == self.active_generation:
            return CredentialAdmission(True, "active_generation")
        if generation == self.previous_generation:
            if now_ns <= int(self.grace_expires_at_ns):
                return CredentialAdmission(True, "rotation_grace")
            return CredentialAdmission(False, "rotation_grace_expired")
        return CredentialAdmission(False, "generation_not_admitted")


class CredentialHandle:
    """Opaque authority reference; it intentionally exposes no document form."""

    __slots__ = ("_entry_id", "_generation", "_owner_pid", "_vault_marker")

    def __init__(
        self,
        *,
        entry_id: object,
        generation: int,
        owner_pid: int,
        vault_marker: object,
    ) -> None:
        self._entry_id = entry_id
        self._generation = generation
        self._owner_pid = owner_pid
        self._vault_marker = vault_marker

    @property
    def generation(self) -> int:
        return self._generation

    def __repr__(self) -> str:
        return "<CredentialHandle opaque>"

    def __reduce__(self) -> object:
        raise TypeError("credential handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("credential handles cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("credential handles cannot be copied")


@dataclass
class _CredentialEntry:
    material: bytearray
    scope: CredentialScope
    generation: int

    def zeroize(self) -> None:
        self.material[:] = b"\x00" * len(self.material)


T = TypeVar("T")


class CredentialVault:
    """Non-serializable in-memory credential store scoped to one process/job."""

    __slots__ = (
        "_closed",
        "_entries",
        "_lock",
        "_marker",
        "_owner_pid",
        "_revoked_through",
    )

    def __init__(self) -> None:
        self._closed = False
        self._entries: dict[object, _CredentialEntry] = {}
        self._lock = threading.RLock()
        self._marker = object()
        self._owner_pid = os.getpid()
        self._revoked_through = 0

    def __enter__(self) -> CredentialVault:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "<CredentialVault closed>"
            if self._closed
            else "<CredentialVault process-local>"
        )

    def __reduce__(self) -> object:
        raise TypeError("credential vaults cannot be serialized")

    def __getstate__(self) -> object:
        raise TypeError("credential vaults cannot be serialized")

    @property
    def debug_live_credential_count(self) -> int:
        """Value-free lifecycle metric; never exposes identifiers or material."""

        with self._lock:
            return len(self._entries)

    def _assert_available(self) -> None:
        if self._closed:
            raise CredentialError("closed")
        if os.getpid() != self._owner_pid:
            raise CredentialError("process_mismatch")

    def issue(
        self,
        material: bytes | bytearray | memoryview,
        *,
        scope: CredentialScope,
        generation: int,
    ) -> CredentialHandle:
        if not isinstance(scope, CredentialScope):
            raise CredentialError("scope_invalid")
        if type(generation) is not int or generation <= 0:
            raise CredentialError("generation_invalid")
        if not isinstance(material, (bytes, bytearray, memoryview)):
            raise CredentialError("material_must_be_bytes")
        copied = bytearray(material)
        if not copied:
            raise CredentialError("material_empty")
        if isinstance(material, bytearray):
            material[:] = b"\x00" * len(material)
        elif isinstance(material, memoryview) and not material.readonly:
            material[:] = b"\x00" * len(material)
        with self._lock:
            self._assert_available()
            if generation <= self._revoked_through:
                copied[:] = b"\x00" * len(copied)
                raise CredentialError("generation_revoked")
            entry_id = object()
            self._entries[entry_id] = _CredentialEntry(
                copied,
                scope,
                generation,
            )
            return CredentialHandle(
                entry_id=entry_id,
                generation=generation,
                owner_pid=self._owner_pid,
                vault_marker=self._marker,
            )

    def _resolve(
        self,
        handle: CredentialHandle,
        scope: CredentialScope,
    ) -> _CredentialEntry:
        self._assert_available()
        if not isinstance(handle, CredentialHandle):
            raise CredentialError("handle_invalid")
        if (
            handle._vault_marker is not self._marker
            or handle._owner_pid != self._owner_pid
        ):
            raise CredentialError("handle_mismatch")
        entry = self._entries.get(handle._entry_id)
        if entry is None:
            if handle._generation <= self._revoked_through:
                raise CredentialError("revoked")
            raise CredentialError("unknown_handle")
        if entry.scope != scope:
            raise CredentialError("scope_mismatch")
        return entry

    def use(
        self,
        handle: CredentialHandle,
        *,
        scope: CredentialScope,
        consumer: Callable[[memoryview], T],
    ) -> T:
        """Invoke a trusted transport consumer with a short-lived readonly view."""

        if not callable(consumer):
            raise CredentialError("consumer_invalid")
        with self._lock:
            entry = self._resolve(handle, scope)
            view = memoryview(entry.material).toreadonly()
            try:
                return consumer(view)
            finally:
                view.release()

    def revoke_through(self, generation: int) -> None:
        if type(generation) is not int or generation < 0:
            raise CredentialError("revocation_generation_invalid")
        with self._lock:
            self._assert_available()
            if generation < self._revoked_through:
                raise CredentialError("revocation_generation_not_monotonic")
            self._revoked_through = generation
            revoked = [
                entry_id
                for entry_id, entry in self._entries.items()
                if entry.generation <= generation
            ]
            for entry_id in revoked:
                entry = self._entries.pop(entry_id)
                entry.zeroize()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for entry in self._entries.values():
                entry.zeroize()
            self._entries.clear()
            self._closed = True
