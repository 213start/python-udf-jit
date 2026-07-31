from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.bundle import (
    BundleRejectCode,
    BundleRunContext,
    BundleStatus,
    DiagnosticBundleError,
    open_bundle,
    read_bundle,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    resolve_diagnostic_policy,
)


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _policy(self, *, max_bytes: int = 1048576):
        return resolve_diagnostic_policy(
            {
                "UDFJIT_DIAGNOSTICS": "summary",
                "UDFJIT_DIAGNOSTIC_DIR": str(self.root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "artifact:abc123",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(max_bytes),
            },
            DiagnosticRuntimeContext(
                workspace_root=self.root / "workspace",
                home_root=self.root / "home",
            ),
        )

    def _writer(self, *, max_bytes: int = 1048576):
        return open_bundle(
            self._policy(max_bytes=max_bytes),
            BundleRunContext(
                run_id="run-1",
                runtime_mode="auto",
                process_key="worker-1",
            ),
        )

    def test_writer_publishes_hashed_read_only_data_with_private_modes(self) -> None:
        writer = self._writer()
        payload = b'{"operation_id":1}'
        artifact = writer.add(
            "semantic/core.final.json",
            "application/json",
            payload,
            {"layer": "semantic"},
        )
        bundle_ref = writer.complete()
        loaded = read_bundle(bundle_ref.path)

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(artifact.byte_size, len(payload))
        self.assertIs(loaded.status, BundleStatus.COMPLETE)
        self.assertTrue((bundle_ref.path / "COMPLETE").is_file())
        self.assertEqual(
            stat.S_IMODE(bundle_ref.path.stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(
                (bundle_ref.path / "semantic" / "core.final.json").stat().st_mode
            ),
            0o600,
        )

    def test_budget_exhaustion_yields_partial_bundle(self) -> None:
        writer = self._writer(max_bytes=2048)
        self.assertIsNotNone(
            writer.add("first.bin", "application/octet-stream", b"a" * 256)
        )
        self.assertIsNone(
            writer.add("second.bin", "application/octet-stream", b"b" * 2048)
        )

        bundle_ref = writer.complete()
        loaded = read_bundle(bundle_ref.path)
        self.assertIs(loaded.status, BundleStatus.PARTIAL)
        self.assertEqual(loaded.manifest["dropped_counts"]["budget_exhausted"], 1)

    def test_abort_publishes_incomplete_without_complete_marker(self) -> None:
        writer = self._writer()
        writer.add("partial.txt", "text/plain", "partial")
        bundle_ref = writer.abort("backend_unavailable")

        loaded = read_bundle(bundle_ref.path)
        self.assertIs(loaded.status, BundleStatus.INCOMPLETE)
        self.assertFalse((bundle_ref.path / "COMPLETE").exists())
        self.assertEqual(
            loaded.manifest["unavailable_reason"],
            "backend_unavailable",
        )

    def test_writer_rejects_absolute_traversal_and_symlink_paths(self) -> None:
        writer = self._writer()
        for path in ("/tmp/escape", "../escape", "nested/../../escape"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(
                    DiagnosticBundleError,
                    BundleRejectCode.PATH_INVALID.value,
                ):
                    writer.add(path, "text/plain", b"x")

        symlink = writer.temporary_path / "linked"
        symlink.symlink_to(self.root)
        with self.assertRaisesRegex(
            DiagnosticBundleError,
            BundleRejectCode.PATH_SYMLINK.value,
        ):
            writer.add("linked/escape", "text/plain", b"x")
        symlink.unlink()
        writer.abort("writer_rejected_path")

    def test_reader_rejects_hash_tampering_and_never_interprets_payload(self) -> None:
        writer = self._writer()
        writer.add(
            "opaque/perf.data",
            "application/octet-stream",
            b"not executable: __reduce__ os.system",
        )
        bundle_ref = writer.complete()
        loaded = read_bundle(bundle_ref.path)
        self.assertEqual(len(loaded.artifacts), 1)

        artifact_path = bundle_ref.path / "opaque" / "perf.data"
        artifact_path.chmod(0o600)
        artifact_path.write_bytes(b"x" * artifact_path.stat().st_size)
        artifact_path.chmod(0o600)
        with self.assertRaisesRegex(
            DiagnosticBundleError,
            BundleRejectCode.HASH_MISMATCH.value,
        ):
            read_bundle(bundle_ref.path)

    def test_reader_rejects_manifest_path_traversal(self) -> None:
        writer = self._writer()
        writer.add("safe.txt", "text/plain", b"safe")
        bundle_ref = writer.complete()
        manifest_path = bundle_ref.path / "manifest.json"
        document = json.loads(manifest_path.read_text(encoding="ascii"))
        document["artifacts"][0]["path"] = "../safe.txt"
        manifest_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")),
            encoding="ascii",
        )
        os.chmod(manifest_path, 0o600)

        with self.assertRaisesRegex(
            DiagnosticBundleError,
            BundleRejectCode.PATH_INVALID.value,
        ):
            read_bundle(bundle_ref.path)


if __name__ == "__main__":
    unittest.main()
