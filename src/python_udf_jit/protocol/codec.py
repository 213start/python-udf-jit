from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from enum import StrEnum
from typing import Any

from python_udf_jit.compiler.region import (
    verify_semantic_region_graph,
)
from python_udf_jit.compiler.verifier import (
    VerificationError,
    VerificationRejectCode,
    verify_semantic_module,
)
from python_udf_jit.protocol.artifact import (
    PortableUdfArtifact,
    artifact_from_documents,
)
from python_udf_jit.protocol.manifest import (
    DEFAULT_MANIFEST,
    ArtifactManifest,
)
from python_udf_jit.protocol.sections import (
    REQUIRED_SECTIONS,
    SectionCodec,
)


ARTIFACT_MAGIC = b"PUJITF1\0"
ARTIFACT_HEADER = struct.Struct(">8sHHII32s")
SECTION_HEADER = struct.Struct(">BHI32s")


class ArtifactRejectCode(StrEnum):
    TOTAL_SIZE_LIMIT = "total_size_limit"
    LENGTH_MISMATCH = "length_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_ENVELOPE = "invalid_envelope"
    SECTION_LIMIT = "section_limit"
    SECTION_SIZE_LIMIT = "section_size_limit"
    MISSING_SECTION = "missing_section"
    DUPLICATE_SECTION = "duplicate_section"
    UNKNOWN_SECTION = "unknown_section"
    UNSUPPORTED_CODEC = "unsupported_codec"
    DUPLICATE_KEY = "duplicate_key"
    DEPTH_LIMIT = "depth_limit"
    STRING_LIMIT = "string_limit"
    INVALID_DOCUMENT = "invalid_document"
    MANIFEST_INCOMPATIBLE = "manifest_incompatible"
    NODE_LIMIT = "node_limit"
    CONSTANT_LIMIT = "constant_limit"
    LAYOUT_UNSUPPORTED = "layout_unsupported"


class ArtifactCodecError(ValueError):
    def __init__(
        self,
        code: ArtifactRejectCode,
        detail: str = "",
    ) -> None:
        self.code = code
        self.detail = detail
        super().__init__(
            code.value if not detail else f"{code.value}:{detail}"
        )


def _fail(code: ArtifactRejectCode, detail: str = "") -> None:
    raise ArtifactCodecError(code, detail)


def _canonical_json(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        _fail(ArtifactRejectCode.INVALID_DOCUMENT, str(error))


def _map_verification(
    error: VerificationError,
) -> ArtifactCodecError:
    if error.code == VerificationRejectCode.NODE_LIMIT:
        code = (
            ArtifactRejectCode.CONSTANT_LIMIT
            if error.detail == "constants"
            else ArtifactRejectCode.NODE_LIMIT
        )
    else:
        code = ArtifactRejectCode.INVALID_DOCUMENT
    return ArtifactCodecError(code, str(error))


def encode_artifact(artifact: PortableUdfArtifact) -> bytes:
    manifest = artifact.manifest
    if not manifest.compatible_with(DEFAULT_MANIFEST):
        _fail(ArtifactRejectCode.MANIFEST_INCOMPATIBLE)
    try:
        verify_semantic_module(
            artifact.semantic_core_module,
            max_nodes=manifest.max_nodes,
            max_constants=manifest.max_constants,
        )
        verify_semantic_region_graph(
            artifact.semantic_core_module,
            artifact.semantic_region_graph,
        )
    except VerificationError as error:
        raise _map_verification(error) from error

    sections = artifact.sections()
    if tuple(section.name for section in sections) != REQUIRED_SECTIONS:
        _fail(ArtifactRejectCode.MISSING_SECTION)
    if len(sections) > manifest.max_sections:
        _fail(ArtifactRejectCode.SECTION_LIMIT)
    body_parts: list[bytes] = []
    for section in sections:
        try:
            section.validate_name(manifest.max_string_bytes)
        except ValueError as error:
            _fail(ArtifactRejectCode.INVALID_ENVELOPE, str(error))
        if section.codec is not SectionCodec.CANONICAL_JSON:
            _fail(ArtifactRejectCode.UNSUPPORTED_CODEC, section.name)
        name_bytes = section.name.encode("ascii")
        document_payload = _canonical_json(section.document)
        if len(document_payload) > manifest.max_section_bytes:
            _fail(ArtifactRejectCode.SECTION_SIZE_LIMIT)
        body_parts.append(
            SECTION_HEADER.pack(
                int(section.codec),
                len(name_bytes),
                len(document_payload),
                hashlib.sha256(document_payload).digest(),
            )
            + name_bytes
            + document_payload
        )
    body = b"".join(body_parts)
    total_length = ARTIFACT_HEADER.size + len(body)
    if total_length > manifest.max_total_bytes:
        _fail(ArtifactRejectCode.TOTAL_SIZE_LIMIT)
    return ARTIFACT_HEADER.pack(
        ARTIFACT_MAGIC,
        manifest.artifact_format_major,
        manifest.artifact_format_minor,
        total_length,
        len(sections),
        hashlib.sha256(body).digest(),
    ) + body


class _DuplicateKey(ValueError):
    pass


def _pairs_no_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _preflight_json(
    payload: bytes,
    manifest: ArtifactManifest,
) -> str:
    if len(payload) > manifest.max_section_bytes:
        _fail(ArtifactRejectCode.SECTION_SIZE_LIMIT)
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        _fail(ArtifactRejectCode.INVALID_DOCUMENT, str(error))
    depth = 0
    in_string = False
    escaped = False
    string_start = 0
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                if index - string_start > manifest.max_string_bytes:
                    _fail(ArtifactRejectCode.STRING_LIMIT)
                in_string = False
            continue
        if character == '"':
            in_string = True
            string_start = index + 1
        elif character in "[{":
            depth += 1
            if depth > manifest.max_json_depth:
                _fail(ArtifactRejectCode.DEPTH_LIMIT)
        elif character in "]}":
            depth -= 1
            if depth < 0:
                _fail(ArtifactRejectCode.INVALID_DOCUMENT)
    if in_string or depth != 0:
        _fail(ArtifactRejectCode.INVALID_DOCUMENT)
    return text


def _decode_json(
    payload: bytes,
    manifest: ArtifactManifest,
) -> Any:
    text = _preflight_json(payload, manifest)
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda value: (
                _ for _ in ()
            ).throw(ValueError(value)),
        )
    except _DuplicateKey as error:
        _fail(ArtifactRejectCode.DUPLICATE_KEY, str(error))
    except (json.JSONDecodeError, ValueError) as error:
        _fail(ArtifactRejectCode.INVALID_DOCUMENT, str(error))


def decode_artifact(
    payload: bytes,
    runtime_manifest: ArtifactManifest = DEFAULT_MANIFEST,
) -> PortableUdfArtifact:
    if not isinstance(payload, bytes):
        _fail(
            ArtifactRejectCode.INVALID_ENVELOPE,
            "payload_not_bytes",
        )
    if len(payload) > runtime_manifest.max_total_bytes:
        _fail(ArtifactRejectCode.TOTAL_SIZE_LIMIT)
    if len(payload) < ARTIFACT_HEADER.size:
        _fail(ArtifactRejectCode.LENGTH_MISMATCH)
    (
        magic,
        major,
        minor,
        declared_total,
        section_count,
        body_hash,
    ) = ARTIFACT_HEADER.unpack_from(payload)
    if magic != ARTIFACT_MAGIC:
        _fail(ArtifactRejectCode.INVALID_ENVELOPE, "magic")
    if (
        major != runtime_manifest.artifact_format_major
        or minor != runtime_manifest.artifact_format_minor
    ):
        _fail(ArtifactRejectCode.UNSUPPORTED_VERSION)
    if declared_total != len(payload):
        _fail(ArtifactRejectCode.LENGTH_MISMATCH)
    if section_count > runtime_manifest.max_sections:
        _fail(ArtifactRejectCode.SECTION_LIMIT)
    body = payload[ARTIFACT_HEADER.size :]
    if hashlib.sha256(body).digest() != body_hash:
        _fail(ArtifactRejectCode.HASH_MISMATCH, "body")

    offset = ARTIFACT_HEADER.size
    documents: dict[str, Any] = {}
    for index in range(section_count):
        if len(payload) - offset < SECTION_HEADER.size:
            _fail(
                ArtifactRejectCode.LENGTH_MISMATCH,
                "section_header",
            )
        (
            codec_value,
            name_length,
            document_length,
            document_hash,
        ) = SECTION_HEADER.unpack_from(payload, offset)
        offset += SECTION_HEADER.size
        if (
            name_length <= 0
            or name_length > runtime_manifest.max_string_bytes
        ):
            _fail(ArtifactRejectCode.STRING_LIMIT)
        if document_length > runtime_manifest.max_section_bytes:
            _fail(ArtifactRejectCode.SECTION_SIZE_LIMIT)
        end = offset + name_length + document_length
        if end > len(payload):
            _fail(
                ArtifactRejectCode.LENGTH_MISMATCH,
                "section_payload",
            )
        try:
            name = payload[
                offset : offset + name_length
            ].decode("ascii")
        except UnicodeDecodeError as error:
            _fail(
                ArtifactRejectCode.INVALID_ENVELOPE,
                str(error),
            )
        offset += name_length
        document_payload = payload[
            offset : offset + document_length
        ]
        offset += document_length
        if hashlib.sha256(document_payload).digest() != document_hash:
            _fail(ArtifactRejectCode.HASH_MISMATCH, name)
        if name in documents:
            _fail(ArtifactRejectCode.DUPLICATE_SECTION, name)
        if index >= len(REQUIRED_SECTIONS) or name != REQUIRED_SECTIONS[index]:
            _fail(ArtifactRejectCode.UNKNOWN_SECTION, name)
        try:
            codec = SectionCodec(codec_value)
        except ValueError:
            _fail(ArtifactRejectCode.UNSUPPORTED_CODEC, name)
        if codec is not SectionCodec.CANONICAL_JSON:
            _fail(ArtifactRejectCode.UNSUPPORTED_CODEC, name)
        documents[name] = _decode_json(
            document_payload,
            runtime_manifest,
        )
    if offset != len(payload):
        _fail(
            ArtifactRejectCode.LENGTH_MISMATCH,
            "trailing_bytes",
        )
    if tuple(documents) != REQUIRED_SECTIONS:
        _fail(ArtifactRejectCode.MISSING_SECTION)
    try:
        artifact = artifact_from_documents(
            documents,
            runtime_manifest,
        )
        return dataclasses.replace(
            artifact,
            encoded_content_sha256=hashlib.sha256(
                payload
            ).hexdigest(),
        )
    except ArtifactCodecError:
        raise
    except VerificationError as error:
        raise _map_verification(error) from error
    except (TypeError, ValueError, KeyError) as error:
        _fail(ArtifactRejectCode.INVALID_DOCUMENT, str(error))
