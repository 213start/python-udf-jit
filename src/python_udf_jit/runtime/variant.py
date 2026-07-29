from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Generic, TypeVar


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True)
class WorkerProcessKey:
    cluster_epoch: str
    node_id: str
    actor_worker_id: str
    pid: int
    process_generation: str

    def __post_init__(self) -> None:
        _require_text(self.cluster_epoch, "cluster epoch")
        _require_text(self.node_id, "node id")
        _require_text(self.actor_worker_id, "actor/worker id")
        _require_text(self.process_generation, "process generation")
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("invalid process pid")


@dataclass(frozen=True)
class VariantKey:
    """Complete U5 key; machine code is never reusable outside this process key."""

    process: WorkerProcessKey
    artifact_content_sha256: str
    semantic_hash: str
    schema_fingerprint: str
    callable_code_sha256: str
    artifact_manifest_sha256: str
    experiment_manifest_sha256: str
    adapter_abi: int
    runtime_abi: int
    scalar_slot_abi: int
    cpython_cinderx_soabi: str
    cpu_features: tuple[str, ...]
    policy_version: str
    policy_sha256: str
    provider_id: str = "scalar-python-cinderx"

    def __post_init__(self) -> None:
        if not isinstance(self.process, WorkerProcessKey):
            raise ValueError("invalid worker process key")
        for field in (
            "semantic_hash",
            "artifact_content_sha256",
            "schema_fingerprint",
            "callable_code_sha256",
            "artifact_manifest_sha256",
            "experiment_manifest_sha256",
            "policy_sha256",
        ):
            value = _require_text(getattr(self, field), field)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"invalid {field}")
        for field in ("adapter_abi", "runtime_abi", "scalar_slot_abi"):
            if type(getattr(self, field)) is not int or getattr(self, field) <= 0:
                raise ValueError(f"invalid {field}")
        _require_text(self.cpython_cinderx_soabi, "CinderX SOABI")
        _require_text(self.provider_id, "provider id")
        _require_text(self.policy_version, "policy version")
        if not isinstance(self.cpu_features, tuple) or not all(
            isinstance(value, str) and value for value in self.cpu_features
        ):
            raise ValueError("invalid CPU features")
        if tuple(sorted(set(self.cpu_features))) != self.cpu_features:
            raise ValueError("CPU features must be sorted and unique")

    @property
    def sha256(self) -> str:
        document = {
            "actor_worker_id": self.process.actor_worker_id,
            "adapter_abi": self.adapter_abi,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "artifact_content_sha256": self.artifact_content_sha256,
            "callable_code_sha256": self.callable_code_sha256,
            "cluster_epoch": self.process.cluster_epoch,
            "cpu_features": list(self.cpu_features),
            "cpython_cinderx_soabi": self.cpython_cinderx_soabi,
            "experiment_manifest_sha256": self.experiment_manifest_sha256,
            "node_id": self.process.node_id,
            "pid": self.process.pid,
            "policy_sha256": self.policy_sha256,
            "policy_version": self.policy_version,
            "process_generation": self.process.process_generation,
            "provider_id": self.provider_id,
            "runtime_abi": self.runtime_abi,
            "scalar_slot_abi": self.scalar_slot_abi,
            "schema_fingerprint": self.schema_fingerprint,
            "semantic_hash": self.semantic_hash,
        }
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


class CacheDecision(StrEnum):
    COMPILE = "compile"
    HIT = "hit"
    MISMATCH = "mismatch"


T = TypeVar("T")


@dataclass(frozen=True)
class CacheResolution(Generic[T]):
    decision: CacheDecision
    value: T | None


class ProcessVariantCache(Generic[T]):
    """One-variant cache owned by exactly one worker process generation."""

    def __init__(self, process: WorkerProcessKey) -> None:
        if not isinstance(process, WorkerProcessKey) or process.pid != os.getpid():
            raise ValueError("variant cache must be created by its owner process")
        self._process = process
        self._key: VariantKey | None = None
        self._value: T | None = None
        self._lock = threading.RLock()

    @property
    def process(self) -> WorkerProcessKey:
        return self._process

    def resolve(self, key: VariantKey, compiler: Callable[[], T]) -> CacheResolution[T]:
        if key.process != self._process or os.getpid() != self._process.pid:
            return CacheResolution(CacheDecision.MISMATCH, None)
        with self._lock:
            if self._key is not None:
                if self._key == key:
                    return CacheResolution(CacheDecision.HIT, self._value)
                return CacheResolution(CacheDecision.MISMATCH, None)
            value = compiler()
            self._key = key
            self._value = value
            return CacheResolution(CacheDecision.COMPILE, value)

    def clear(self, closer: Callable[[T], None] | None = None) -> None:
        with self._lock:
            value = self._value
            self._key = None
            self._value = None
        if value is not None and closer is not None:
            closer(value)
