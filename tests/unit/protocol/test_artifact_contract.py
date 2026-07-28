from __future__ import annotations

import hashlib
import struct
import unittest

from python_udf_jit.protocol.codec import (
    ARTIFACT_HEADER,
    ARTIFACT_MAGIC,
    ArtifactCodecError,
    ArtifactRejectCode,
    decode_artifact,
    encode_artifact,
)
from python_udf_jit.protocol.manifest import DEFAULT_MANIFEST
from tests.unit.protocol.test_artifact_codec import (
    artifact,
    raw_envelope,
)


class FormalArtifactContractTest(unittest.TestCase):
    def test_formal_format_separates_portable_and_target_manifests(self):
        built = artifact()
        documents = built.section_documents()

        self.assertEqual(built.manifest.artifact_format_major, 1)
        self.assertEqual(built.manifest.artifact_format_minor, 0)
        self.assertIn("manifest", documents)
        self.assertIn("target", documents)
        self.assertNotIn("target_python", documents["manifest"])
        self.assertEqual(documents["target"]["target_python"], "3.14.3")
        self.assertEqual(decode_artifact(encode_artifact(built)), built)

    def test_any_other_major_or_minor_is_rejected(self):
        encoded = encode_artifact(artifact())
        cases = []
        for offset, value in (
            (len(ARTIFACT_MAGIC), 2),
            (len(ARTIFACT_MAGIC) + 2, 1),
        ):
            changed = bytearray(encoded)
            struct.pack_into(">H", changed, offset, value)
            cases.append(bytes(changed))

        for payload in cases:
            with self.subTest(payload=payload[:16]):
                with self.assertRaises(ArtifactCodecError) as raised:
                    decode_artifact(payload)
                self.assertEqual(
                    raised.exception.code,
                    ArtifactRejectCode.UNSUPPORTED_VERSION,
                )

    def test_unknown_section_is_rejected_without_forward_compatibility(self):
        documents = list(artifact().section_documents().items())
        cases = {
            "unknown_future_section": documents
            + [("future_hint", {"value": 1})],
            "piercing_sections_are_not_a_previous_version": [
                ("manifest", documents[0][1]),
                ("core_ir", {"format_version": 1}),
                ("region", {"format_version": 1}),
                ("guard", {}),
                ("fallback", {}),
            ],
        }

        for name, payload_documents in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ArtifactCodecError) as raised:
                    decode_artifact(raw_envelope(payload_documents))
                self.assertEqual(
                    raised.exception.code,
                    ArtifactRejectCode.UNKNOWN_SECTION,
                )

    def test_unknown_manifest_field_is_rejected(self):
        documents = artifact().section_documents()
        changed = dict(documents)
        portable = dict(changed["manifest"])
        portable["future_field"] = 1
        changed["manifest"] = portable

        with self.assertRaises(ArtifactCodecError) as raised:
            decode_artifact(raw_envelope(list(changed.items())))

        self.assertEqual(
            raised.exception.code,
            ArtifactRejectCode.INVALID_DOCUMENT,
        )

    def test_unknown_codec_is_rejected_before_document_decode(self):
        mutable = bytearray(encode_artifact(artifact()))
        mutable[ARTIFACT_HEADER.size] = 255
        body = bytes(mutable[ARTIFACT_HEADER.size :])
        body_hash_offset = ARTIFACT_HEADER.size - 32
        mutable[
            body_hash_offset : body_hash_offset + 32
        ] = hashlib.sha256(body).digest()

        with self.assertRaises(ArtifactCodecError) as raised:
            decode_artifact(bytes(mutable))

        self.assertEqual(
            raised.exception.code,
            ArtifactRejectCode.UNSUPPORTED_CODEC,
        )


if __name__ == "__main__":
    unittest.main()
