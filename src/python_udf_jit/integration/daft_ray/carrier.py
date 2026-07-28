from __future__ import annotations

import hashlib
import base64
import json
from dataclasses import dataclass
from typing import Any, Callable


class CarrierContractError(ValueError):
    """Raised when carrier state or runtime evidence is not trustworthy."""


DEFAULT_INLINE_ARTIFACT_THRESHOLD = 4096


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CarrierContractError(f"{field} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class InlineArtifactHandle:
    kind: str
    content_sha256: str
    size_bytes: int
    payload: bytes | None = None


@dataclass(frozen=True)
class ObjectRefArtifactHandle:
    """Opaque framework object reference plus content-addressed metadata."""

    kind: str
    content_sha256: str
    size_bytes: int
    reference: object


ArtifactHandle = InlineArtifactHandle | ObjectRefArtifactHandle


@dataclass(frozen=True)
class ScalarCallView:
    """Address-free metadata presented at the Driver/Worker call boundary."""

    schema_version: int
    candidate_id: str
    usage_context: str
    logical_schema_sha256: str
    carrier_schema_version: int
    handle_kind: str
    content_sha256: str
    size_bytes: int

    @classmethod
    def from_carrier(
        cls,
        *,
        candidate_id: str,
        usage_context: str,
        logical_schema: str,
        carrier: "ProductionCarrierState",
    ) -> "ScalarCallView":
        view = cls(
            schema_version=1,
            candidate_id=candidate_id,
            usage_context=usage_context,
            logical_schema_sha256=_sha256(logical_schema.encode("utf-8")),
            carrier_schema_version=carrier.schema_version,
            handle_kind=carrier.handle.kind,
            content_sha256=carrier.handle.content_sha256,
            size_bytes=carrier.handle.size_bytes,
        )
        view._validate()
        return view

    def to_bytes(self) -> bytes:
        self._validate()
        return json.dumps(
            {
                "candidate_id": self.candidate_id,
                "carrier_schema_version": self.carrier_schema_version,
                "content_sha256": self.content_sha256,
                "handle_kind": self.handle_kind,
                "logical_schema_sha256": self.logical_schema_sha256,
                "schema_version": self.schema_version,
                "size_bytes": self.size_bytes,
                "usage_context": self.usage_context,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ScalarCallView":
        try:
            document = json.loads(payload.decode("ascii"))
            expected = {
                "candidate_id",
                "carrier_schema_version",
                "content_sha256",
                "handle_kind",
                "logical_schema_sha256",
                "schema_version",
                "size_bytes",
                "usage_context",
            }
            if type(document) is not dict or set(document) != expected:
                raise CarrierContractError(
                    "scalar call view fields do not match schema v1"
                )
            view = cls(
                schema_version=document["schema_version"],
                candidate_id=document["candidate_id"],
                usage_context=document["usage_context"],
                logical_schema_sha256=document["logical_schema_sha256"],
                carrier_schema_version=document["carrier_schema_version"],
                handle_kind=document["handle_kind"],
                content_sha256=document["content_sha256"],
                size_bytes=document["size_bytes"],
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise CarrierContractError(f"invalid scalar call view: {error}") from error
        view._validate()
        return view

    def _validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.carrier_schema_version) is not int
            or self.carrier_schema_version != 1
        ):
            raise CarrierContractError("unsupported scalar call view version")
        if (
            type(self.candidate_id) is not str
            or not self.candidate_id
            or type(self.usage_context) is not str
            or self.usage_context not in {"filter", "selection", "projection"}
            or type(self.handle_kind) is not str
            or self.handle_kind
            not in {"placeholder", "inline-artifact", "object-ref"}
            or type(self.size_bytes) is not int
            or self.size_bytes < 0
            or type(self.logical_schema_sha256) is not str
            or type(self.content_sha256) is not str
        ):
            raise CarrierContractError("invalid scalar call view")
        _require_sha256(self.logical_schema_sha256, "logical_schema_sha256")
        _require_sha256(self.content_sha256, "content_sha256")


@dataclass(frozen=True)
class ProductionCarrierState:
    """Framework-neutral state container reused by later Daft hook/artifact units."""

    schema_version: int
    candidate_id: str
    manifest_sha256: str
    handle: ArtifactHandle

    @classmethod
    def placeholder(
        cls, candidate_id: str, manifest_sha256: str
    ) -> "ProductionCarrierState":
        if not candidate_id:
            raise CarrierContractError("candidate_id must not be empty")
        _require_sha256(manifest_sha256, "manifest_sha256")
        placeholder_payload = (
            f"python-udf-jit:placeholder:v1:{candidate_id}:{manifest_sha256}"
        ).encode("utf-8")
        return cls(
            schema_version=1,
            candidate_id=candidate_id,
            manifest_sha256=manifest_sha256,
            handle=InlineArtifactHandle("placeholder", _sha256(placeholder_payload), 0),
        )

    @property
    def finalized(self) -> bool:
        return self.handle.kind in {"inline-artifact", "object-ref"}

    @property
    def state_sha256(self) -> str:
        payload = json.dumps(
            self._document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return _sha256(payload)

    def finalize(
        self,
        artifact: bytes,
        *,
        inline_threshold: int = 65536,
        publisher: Callable[[bytes], object] | None = None,
    ) -> "ProductionCarrierState":
        if not isinstance(artifact, bytes) or not artifact:
            raise CarrierContractError("artifact must be non-empty bytes")
        if type(inline_threshold) is not int or inline_threshold < 0:
            raise CarrierContractError(
                "inline_threshold must be a non-negative integer"
            )
        artifact_hash = _sha256(artifact)
        if self.finalized:
            if self.handle.content_sha256 != artifact_hash or self.handle.size_bytes != len(artifact):
                raise CarrierContractError("carrier is already finalized with a different artifact")
            return self
        if len(artifact) > inline_threshold:
            if publisher is None:
                raise CarrierContractError(
                    "large artifact requires an object-store publisher"
                )
            try:
                reference = publisher(artifact)
            except Exception as error:
                raise CarrierContractError(
                    f"object-store publication failed: {type(error).__name__}"
                ) from error
            if reference is None:
                raise CarrierContractError(
                    "object-store publisher returned no reference"
                )
            handle: ArtifactHandle = ObjectRefArtifactHandle(
                "object-ref",
                artifact_hash,
                len(artifact),
                reference,
            )
        else:
            handle = InlineArtifactHandle(
                "inline-artifact",
                artifact_hash,
                len(artifact),
                artifact,
            )
        return ProductionCarrierState(
            self.schema_version,
            self.candidate_id,
            self.manifest_sha256,
            handle,
        )

    @property
    def artifact_bytes(self) -> bytes:
        if (
            not self.finalized
            or not isinstance(self.handle, InlineArtifactHandle)
            or self.handle.payload is None
        ):
            raise CarrierContractError("carrier does not contain a finalized inline artifact")
        return self.handle.payload

    def _document(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "handle": {
                "content_sha256": self.handle.content_sha256,
                "kind": self.handle.kind,
                "payload_b64": self._payload_b64(),
                "size_bytes": self.handle.size_bytes,
            },
            "manifest_sha256": self.manifest_sha256,
            "schema_version": self.schema_version,
        }

    def _payload_b64(self) -> str | None:
        if isinstance(self.handle, ObjectRefArtifactHandle):
            return None
        return (
            None
            if self.handle.payload is None
            else base64.b64encode(self.handle.payload).decode("ascii")
        )

    def to_bytes(self) -> bytes:
        if isinstance(self.handle, ObjectRefArtifactHandle):
            raise CarrierContractError(
                "object reference stays in the framework carrier"
            )
        return json.dumps(
            self._document(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ProductionCarrierState":
        try:
            document = json.loads(payload.decode("ascii"))
            if set(document) != {"candidate_id", "handle", "manifest_sha256", "schema_version"}:
                raise CarrierContractError("carrier envelope fields do not match schema v1")
            handle_doc = document["handle"]
            if set(handle_doc) != {
                "content_sha256",
                "kind",
                "payload_b64",
                "size_bytes",
            }:
                raise CarrierContractError("carrier handle fields do not match schema v1")
            payload_b64 = handle_doc["payload_b64"]
            if payload_b64 is None:
                artifact_payload = None
            elif isinstance(payload_b64, str):
                artifact_payload = base64.b64decode(payload_b64, validate=True)
            else:
                raise CarrierContractError("carrier payload encoding is invalid")
            state = cls(
                schema_version=int(document["schema_version"]),
                candidate_id=str(document["candidate_id"]),
                manifest_sha256=str(document["manifest_sha256"]),
                handle=InlineArtifactHandle(
                    str(handle_doc["kind"]),
                    str(handle_doc["content_sha256"]),
                    int(handle_doc["size_bytes"]),
                    artifact_payload,
                ),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            base64.binascii.Error,
        ) as error:
            raise CarrierContractError(f"invalid carrier envelope: {error}") from error
        state._validate()
        return state

    def _validate(self) -> None:
        if self.schema_version != 1 or not self.candidate_id:
            raise CarrierContractError("unsupported or incomplete carrier state")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_sha256(self.handle.content_sha256, "handle.content_sha256")
        if self.handle.kind not in {
            "placeholder",
            "inline-artifact",
            "object-ref",
        }:
            raise CarrierContractError("unsupported carrier handle kind")
        if self.handle.size_bytes < 0 or (
            self.handle.kind == "placeholder" and self.handle.size_bytes != 0
        ):
            raise CarrierContractError("invalid carrier handle size")
        if self.handle.kind == "placeholder":
            if not isinstance(self.handle, InlineArtifactHandle):
                raise CarrierContractError(
                    "placeholder must use an inline handle"
                )
            if self.handle.payload is not None:
                raise CarrierContractError("placeholder carrier must not contain payload bytes")
        elif self.handle.kind == "inline-artifact":
            if (
                not isinstance(self.handle, InlineArtifactHandle)
                or self.handle.payload is None
                or len(self.handle.payload) != self.handle.size_bytes
                or _sha256(self.handle.payload)
                != self.handle.content_sha256
            ):
                raise CarrierContractError(
                    "inline artifact payload does not match its hash and size"
                )
        elif (
            not isinstance(self.handle, ObjectRefArtifactHandle)
            or self.handle.reference is None
            or self.handle.size_bytes <= 0
        ):
            raise CarrierContractError(
                "object-ref artifact handle is incomplete"
            )


@dataclass(frozen=True)
class ExecutionCarrierObservation:
    carrier_kind: str
    actor_or_worker_id: str
    node_id: str
    pid: int
    process_generation: str
    required_cpus: float


def validate_execution_carrier(
    observation: ExecutionCarrierObservation,
    worker_node_ids: set[str] | frozenset[str],
) -> None:
    if observation.required_cpus <= 0:
        raise CarrierContractError("real Daft execution carrier must request logical CPU")
    if observation.node_id not in worker_node_ids:
        raise CarrierContractError("real Daft execution carrier must run on a Worker node")
    if not observation.actor_or_worker_id or observation.pid <= 0 or not observation.process_generation:
        raise CarrierContractError("execution carrier process identity is incomplete")
