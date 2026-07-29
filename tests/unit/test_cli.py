from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.cli import main
from python_udf_jit.protocol.codec import encode_artifact
from tests.unit.protocol.test_artifact_codec import artifact


class CliTests(unittest.TestCase):
    def test_artifact_verify_is_private_and_never_maps_machine_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.udfjit"
            path.write_bytes(encode_artifact(artifact()))
            path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["artifact", "verify", str(path)])

        document = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(document["status"], "pass")
        self.assertFalse(document["machine_code_mapped"])
        self.assertEqual(
            (document["format_major"], document["format_minor"]),
            (1, 0),
        )

    def test_artifact_verify_rejects_symlink_and_public_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "artifact.udfjit"
            target.write_bytes(encode_artifact(artifact()))
            target.chmod(0o644)
            link = root / "link.udfjit"
            link.symlink_to(target)
            for path in (target, link):
                with self.subTest(path=path.name):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        status = main(
                            ["artifact", "verify", os.fspath(path)]
                        )
                    self.assertEqual(status, 2)
                    self.assertEqual(
                        json.loads(output.getvalue())["status"],
                        "fail",
                    )
