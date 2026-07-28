from __future__ import annotations

import hashlib
import importlib.metadata
import secrets
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from python_udf_jit.protocol.artifact import PortableUdfArtifact
from python_udf_jit.protocol.codec import (
    ArtifactCodecError,
    decode_artifact,
)
from python_udf_jit.protocol.manifest import (
    DEFAULT_MANIFEST,
    ArtifactManifest,
)


_RAY_GET_TIMEOUT_SECONDS = 30.0
_PREFETCH_LOCK = threading.RLock()
_PREFETCHED_PAYLOADS: dict[
    tuple[str, str, str],
    bytes,
] = {}


class ArtifactLoadRejectCode(StrEnum):
    HANDLE_INVALID = "handle_invalid"
    OBJECT_MISSING = "object_missing"
    CONTENT_MISMATCH = "content_mismatch"
    CODEC_REJECTED = "codec_rejected"
    DEPENDENCY_MISSING = "dependency_missing"
    DEPENDENCY_VERSION_MISMATCH = "dependency_version_mismatch"


class ArtifactLoadError(ValueError):
    def __init__(
        self,
        code: ArtifactLoadRejectCode,
        detail: str = "",
    ) -> None:
        self.code = code
        self.detail = detail
        super().__init__(
            code.value if not detail else f"{code.value}:{detail}"
        )


@dataclass(frozen=True)
class LoaderNamespace:
    job_namespace: str
    tenant_namespace: str
    process_generation: str

    def __post_init__(self) -> None:
        values = (
            self.job_namespace,
            self.tenant_namespace,
            self.process_generation,
        )
        if any(
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 4096
            for value in values
        ):
            raise ValueError("loader namespace values must be bounded strings")


@dataclass(frozen=True)
class _LoaderKey:
    namespace: LoaderNamespace
    content_sha256: str


def _installed_distribution_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _ray_get(reference: object) -> bytes:
    # Ray may dereference an ObjectRef nested in a task argument before the
    # user function sees it. The content hash and declared size are still
    # verified by ArtifactLoader before decoding.
    if isinstance(reference, bytes):
        return reference

    import ray

    try:
        actor_id = ray.get_runtime_context().get_actor_id()
        is_nil = getattr(actor_id, "is_nil", None)
        inside_actor = (
            not is_nil()
            if callable(is_nil)
            else isinstance(actor_id, str) and bool(actor_id)
        )
    except Exception:
        inside_actor = False
    if inside_actor:
        raise RuntimeError(
            "async_actor_object_ref_was_not_prefetched"
        )

    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        value = ray.get(
            reference,
            timeout=_RAY_GET_TIMEOUT_SECONDS,
        )
    else:
        raise RuntimeError(
            "async_actor_object_ref_was_not_prefetched"
        )
    if not isinstance(value, bytes):
        raise TypeError("Ray object does not contain artifact bytes")
    return value


def prefetch_artifact_payload(
    *,
    job_namespace: str,
    tenant_namespace: str,
    content_sha256: str,
    payload: bytes,
) -> None:
    namespace = LoaderNamespace(
        job_namespace,
        tenant_namespace,
        "prefetch",
    )
    if (
        not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in content_sha256
        )
        or not isinstance(payload, bytes)
        or not payload
        or len(payload) > DEFAULT_MANIFEST.max_total_bytes
    ):
        raise ValueError("invalid prefetched artifact payload")
    key = (
        namespace.job_namespace,
        namespace.tenant_namespace,
        content_sha256,
    )
    with _PREFETCH_LOCK:
        existing = _PREFETCHED_PAYLOADS.get(key)
        if existing is not None and existing != payload:
            raise ValueError("prefetched artifact content collision")
        _PREFETCHED_PAYLOADS[key] = payload


def clear_prefetched_artifact_payloads(
    *,
    job_namespace: str,
    tenant_namespace: str,
    content_sha256s: tuple[str, ...],
) -> None:
    with _PREFETCH_LOCK:
        for content_sha256 in content_sha256s:
            _PREFETCHED_PAYLOADS.pop(
                (
                    job_namespace,
                    tenant_namespace,
                    content_sha256,
                ),
                None,
            )


def _prefetched_payload(
    namespace: LoaderNamespace,
    content_sha256: str,
) -> bytes | None:
    with _PREFETCH_LOCK:
        return _PREFETCHED_PAYLOADS.get(
            (
                namespace.job_namespace,
                namespace.tenant_namespace,
                content_sha256,
            )
        )


class ArtifactLoader:
    """Process-local, namespace-isolated positive and negative artifact cache."""

    def __init__(
        self,
        *,
        resolver: Callable[[object], bytes] | None = None,
        decoder: Callable[
            [bytes, ArtifactManifest],
            PortableUdfArtifact,
        ] = decode_artifact,
        dependency_resolver: Callable[[str], str | None] = (
            _installed_distribution_version
        ),
        runtime_manifest: ArtifactManifest = DEFAULT_MANIFEST,
    ) -> None:
        self._resolver = resolver or _ray_get
        self._decoder = decoder
        self._dependency_resolver = dependency_resolver
        self._runtime_manifest = runtime_manifest
        self._positive: dict[_LoaderKey, PortableUdfArtifact] = {}
        self._negative: dict[_LoaderKey, ArtifactLoadError] = {}
        self._lock = threading.RLock()

    def load(
        self,
        handle: Any,
        namespace: LoaderNamespace,
    ) -> PortableUdfArtifact:
        content_sha256 = getattr(handle, "content_sha256", None)
        if (
            not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in content_sha256
            )
        ):
            raise ArtifactLoadError(
                ArtifactLoadRejectCode.HANDLE_INVALID,
                "content_sha256",
            )
        key = _LoaderKey(namespace, content_sha256)
        with self._lock:
            cached = self._positive.get(key)
            if cached is not None:
                return cached
            rejected = self._negative.get(key)
            if rejected is not None:
                raise rejected
            try:
                artifact = self._load_uncached(
                    handle,
                    namespace,
                )
            except ArtifactLoadError as error:
                self._negative[key] = error
                raise
            self._positive[key] = artifact
            return artifact

    def _load_uncached(
        self,
        handle: Any,
        namespace: LoaderNamespace,
    ) -> PortableUdfArtifact:
        kind = getattr(handle, "kind", None)
        declared_size = getattr(handle, "size_bytes", None)
        if (
            kind not in {"inline-artifact", "object-ref"}
            or type(declared_size) is not int
            or declared_size <= 0
            or declared_size > self._runtime_manifest.max_total_bytes
        ):
            raise ArtifactLoadError(
                ArtifactLoadRejectCode.HANDLE_INVALID,
                "kind_or_size",
            )
        if kind == "inline-artifact":
            payload = getattr(handle, "payload", None)
        else:
            payload = _prefetched_payload(
                namespace,
                getattr(handle, "content_sha256", ""),
            )
            if payload is None:
                reference = getattr(handle, "reference", None)
                if reference is None:
                    raise ArtifactLoadError(
                        ArtifactLoadRejectCode.HANDLE_INVALID,
                        "object_reference",
                    )
                try:
                    payload = self._resolver(reference)
                except Exception as error:
                    raise ArtifactLoadError(
                        ArtifactLoadRejectCode.OBJECT_MISSING,
                        type(error).__name__,
                    ) from error
        if (
            not isinstance(payload, bytes)
            or len(payload) != declared_size
            or not secrets.compare_digest(
                hashlib.sha256(payload).hexdigest(),
                getattr(handle, "content_sha256", ""),
            )
        ):
            raise ArtifactLoadError(
                ArtifactLoadRejectCode.CONTENT_MISMATCH,
            )
        try:
            artifact = self._decoder(payload, self._runtime_manifest)
        except ArtifactCodecError as error:
            raise ArtifactLoadError(
                ArtifactLoadRejectCode.CODEC_REJECTED,
                (
                    error.code.value
                    if not error.detail
                    else f"{error.code.value}:{error.detail}"
                ),
            ) from error
        except Exception as error:
            raise ArtifactLoadError(
                ArtifactLoadRejectCode.CODEC_REJECTED,
                type(error).__name__,
            ) from error
        self._admit_dependencies(artifact)
        return artifact

    def _admit_dependencies(
        self,
        artifact: PortableUdfArtifact,
    ) -> None:
        for requirement in artifact.manifest.dependency_requirements:
            actual = self._dependency_resolver(requirement.distribution)
            if actual is None:
                raise ArtifactLoadError(
                    ArtifactLoadRejectCode.DEPENDENCY_MISSING,
                    requirement.distribution,
                )
            if actual != requirement.version:
                raise ArtifactLoadError(
                    ArtifactLoadRejectCode.DEPENDENCY_VERSION_MISMATCH,
                    requirement.distribution,
                )

    def clear_namespace(self, namespace: LoaderNamespace) -> None:
        with self._lock:
            self._positive = {
                key: value
                for key, value in self._positive.items()
                if key.namespace != namespace
            }
            self._negative = {
                key: value
                for key, value in self._negative.items()
                if key.namespace != namespace
            }

    @property
    def positive_entry_count(self) -> int:
        with self._lock:
            return len(self._positive)

    @property
    def negative_entry_count(self) -> int:
        with self._lock:
            return len(self._negative)
