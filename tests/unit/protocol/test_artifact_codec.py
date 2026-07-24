from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture, reference_execute
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.protocol.artifact import PortableUdfArtifact, build_artifact
from python_udf_jit.protocol.codec import (
    ARTIFACT_HEADER,
    ARTIFACT_MAGIC,
    SECTION_HEADER,
    ArtifactCodecError,
    ArtifactRejectCode,
    decode_artifact,
    encode_artifact,
)
from python_udf_jit.protocol.manifest import DEFAULT_MANIFEST, ArtifactManifest


def affine(x):
    return x * 2.0 + 3.0


def artifact() -> PortableUdfArtifact:
    module = lower_capture(capture(CaptureRequest(affine)))
    return build_artifact(module, form_verified_region(module), module.fallback_identity)


def raw_envelope(section_documents, *, major=1, minor=0):
    body_parts = []
    for name, document in section_documents:
        name_bytes = name.encode("ascii")
        if isinstance(document, bytes):
            payload = document
        else:
            payload = json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        body_parts.append(
            SECTION_HEADER.pack(len(name_bytes), len(payload), hashlib.sha256(payload).digest())
            + name_bytes
            + payload
        )
    body = b"".join(body_parts)
    total = ARTIFACT_HEADER.size + len(body)
    return ARTIFACT_HEADER.pack(
        ARTIFACT_MAGIC, major, minor, total, len(section_documents), hashlib.sha256(body).digest()
    ) + body


class ArtifactCodecTest(unittest.TestCase):
    def test_encoding_is_deterministic_content_addressed_and_pickle_free(self):
        first = encode_artifact(artifact())
        second = encode_artifact(artifact())

        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first).hexdigest(), artifact().content_sha256)
        self.assertNotIn(b"pickle", first.lower())
        for forbidden in (b"address", b"descriptor", b"hir", b"lir", b"machine_code", b"source"):
            self.assertNotIn(forbidden, first.lower())

    def test_roundtrip_across_an_independent_python_process(self):
        encoded = encode_artifact(artifact())
        script = """
import base64, json, os
from python_udf_jit.compiler.core_ir import reference_execute
from python_udf_jit.protocol.codec import decode_artifact
loaded = decode_artifact(base64.b64decode(os.environ['ARTIFACT']))
print(json.dumps({'hash': loaded.content_sha256, 'result': reference_execute(loaded.core_module, 4.0)}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ, ARTIFACT=base64.b64encode(encoded).decode("ascii")),
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["hash"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(result["result"], 11.0)

    def test_rejects_truncation_hash_tampering_and_newer_version(self):
        encoded = encode_artifact(artifact())
        cases = []
        cases.append((encoded[:-1], ArtifactRejectCode.LENGTH_MISMATCH))
        tampered = bytearray(encoded)
        tampered[-1] ^= 1
        cases.append((bytes(tampered), ArtifactRejectCode.HASH_MISMATCH))
        newer = bytearray(encoded)
        struct.pack_into(">H", newer, len(ARTIFACT_MAGIC), 2)
        cases.append((bytes(newer), ArtifactRejectCode.UNSUPPORTED_VERSION))

        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ArtifactCodecError) as raised:
                    decode_artifact(payload)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_missing_fields_duplicate_keys_and_incompatible_manifest(self):
        valid = artifact().section_documents()
        missing_core = dict(valid)
        missing_core["core_ir"] = {"format_version": 1}

        duplicate_manifest = b'{"artifact_format_major":1,"artifact_format_major":1}'

        incompatible = dict(valid)
        manifest = dict(incompatible["manifest"])
        manifest["target_python"] = "3.14.4"
        incompatible["manifest"] = manifest

        cases = (
            (
                raw_envelope(list(missing_core.items())),
                ArtifactRejectCode.INVALID_DOCUMENT,
            ),
            (
                raw_envelope(
                    [(name, duplicate_manifest if name == "manifest" else document) for name, document in valid.items()]
                ),
                ArtifactRejectCode.DUPLICATE_KEY,
            ),
            (
                raw_envelope(list(incompatible.items())),
                ArtifactRejectCode.MANIFEST_INCOMPATIBLE,
            ),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ArtifactCodecError) as raised:
                    decode_artifact(payload)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_budget_overruns_before_semantic_use(self):
        valid = artifact().section_documents()

        too_many_sections = [(f"s{index}", {}) for index in range(DEFAULT_MANIFEST.max_sections + 1)]
        too_deep = dict(valid)
        nested = value = {}
        for _ in range(DEFAULT_MANIFEST.max_json_depth + 1):
            value["x"] = {}
            value = value["x"]
        too_deep["guard"] = nested
        too_long = dict(valid)
        too_long["fallback"] = {
            "module": "x" * (DEFAULT_MANIFEST.max_string_bytes + 1),
            "qualname": "affine",
            "code_sha256": "0" * 64,
        }

        cases = (
            (raw_envelope(too_many_sections), ArtifactRejectCode.SECTION_LIMIT),
            (raw_envelope(list(too_deep.items())), ArtifactRejectCode.DEPTH_LIMIT),
            (raw_envelope(list(too_long.items())), ArtifactRejectCode.STRING_LIMIT),
            (b"x" * (DEFAULT_MANIFEST.max_total_bytes + 1), ArtifactRejectCode.TOTAL_SIZE_LIMIT),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ArtifactCodecError) as raised:
                    decode_artifact(payload)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_node_and_constant_limits_at_encode(self):
        built = artifact()
        nodes = built.core_module.nodes
        oversized = built.with_core_nodes(nodes * 60)

        with self.assertRaises(ArtifactCodecError) as raised:
            encode_artifact(oversized)
        self.assertIn(
            raised.exception.code,
            {ArtifactRejectCode.NODE_LIMIT, ArtifactRejectCode.CONSTANT_LIMIT},
        )

    def test_manifest_target_stays_locked_to_remote_python_3_14_3(self):
        self.assertEqual(DEFAULT_MANIFEST.target_python, "3.14.3")
        document = DEFAULT_MANIFEST.to_document()
        self.assertEqual(ArtifactManifest.from_document(document), DEFAULT_MANIFEST)


if __name__ == "__main__":
    unittest.main()
