from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.system.private_output import (
    write_private_bytes,
    write_private_json,
)


class PrivateOutputTests(unittest.TestCase):
    def test_existing_parent_permissions_are_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "caller-owned"
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)
            output = parent / "proof.json"

            write_private_json(output, {"status": "pass"})

            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_new_parent_and_output_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "new" / "nested"
            output = parent / "proof.bin"

            write_private_bytes(output, b"proof")

            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "proof.bin"
            write_private_bytes(output, b"first")

            with self.assertRaises(FileExistsError):
                write_private_bytes(output, b"second")

            self.assertEqual(output.read_bytes(), b"first")


if __name__ == "__main__":
    unittest.main()
