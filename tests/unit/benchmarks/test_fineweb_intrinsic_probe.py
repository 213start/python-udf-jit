from __future__ import annotations

import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "benchmarks/fineweb_alnum_intrinsic_probe.py"


def _load_probe_module():
    cinderx = types.ModuleType("cinderx")
    jit = types.ModuleType("cinderx.jit")
    cinderx.jit = jit
    extension = types.ModuleType("_fineweb_alnum_probe")
    ops = types.ModuleType("ops")
    datajuicer = types.ModuleType("ops.datajuicer_cpu_text_ops")
    for name in (
        "dj_alphanumeric_ok",
        "dj_punctuation_normalize",
        "dj_whitespace_normalization",
    ):
        setattr(datajuicer, name, lambda value, **_kwargs: value)
    spec = importlib.util.spec_from_file_location(
        "fineweb_intrinsic_probe_under_test",
        PROBE,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("probe module spec unavailable")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "cinderx": cinderx,
            "cinderx.jit": jit,
            "_fineweb_alnum_probe": extension,
            "ops": ops,
            "ops.datajuicer_cpu_text_ops": datajuicer,
        },
    ):
        spec.loader.exec_module(module)
    return module


class FineWebIntrinsicProbeInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = _load_probe_module()

    @staticmethod
    def _write(path: Path, values: list[object]) -> bytes:
        payload = b"".join(
            json.dumps({"text": value}).encode("utf-8") + b"\n"
            for value in values
        )
        path.write_bytes(payload)
        return payload

    def test_exact_row_and_byte_limits_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.jsonl")
            payload = self._write(path, ["one", "two"])

            texts, byte_count = self.probe._load_texts(
                path,
                max_rows=2,
                max_input_bytes=len(payload),
            )

        self.assertEqual(texts, ["one", "two"])
        self.assertEqual(byte_count, len(payload))

    def test_row_and_byte_overflow_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.jsonl")
            payload = self._write(path, ["one", "two"])
            with self.assertRaisesRegex(ValueError, "input_row_limit_exceeded"):
                self.probe._load_texts(
                    path,
                    max_rows=1,
                    max_input_bytes=len(payload),
                )
            with self.assertRaisesRegex(ValueError, "input_byte_limit_exceeded"):
                self.probe._load_texts(
                    path,
                    max_rows=2,
                    max_input_bytes=len(payload) - 1,
                )

    def test_oversized_unterminated_record_is_bounded_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.jsonl")
            path.write_bytes(b"{" + b"x" * 1024 * 1024)

            with self.assertRaisesRegex(ValueError, "input_byte_limit_exceeded"):
                self.probe._load_texts(
                    path,
                    max_rows=1,
                    max_input_bytes=32,
                )

    def test_non_string_text_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.jsonl")
            payload = self._write(path, [1])

            with self.assertRaisesRegex(TypeError, "input_text_not_string"):
                self.probe._load_texts(
                    path,
                    max_rows=1,
                    max_input_bytes=len(payload),
                )

    def test_input_evidence_uses_content_id_without_local_path(self) -> None:
        private_root = "/benchmark-private-root/"
        private_path = Path(private_root, "fineweb.jsonl")
        with mock.patch.object(
            self.probe,
            "_sha256_file",
            return_value="a" * 64,
        ):
            evidence = self.probe._input_evidence(
                private_path,
                rows=2,
                input_bytes=42,
                characters=37,
                max_rows=10_000,
                max_input_bytes=64 * 1024 * 1024,
                min_ratio=0.2,
            )

        serialized = json.dumps(evidence, sort_keys=True)
        self.assertEqual(evidence["artifact_id"], f"sha256:{'a' * 64}")
        self.assertNotIn("path", evidence)
        self.assertNotIn(str(private_path), serialized)
        self.assertNotIn(private_root, serialized)


if __name__ == "__main__":
    unittest.main()
