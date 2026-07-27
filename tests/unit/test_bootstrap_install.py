from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.bootstrap_install import (
    BootstrapInstallError,
    install_bootstrap,
)


class BootstrapInstallTests(unittest.TestCase):
    def test_installs_packaged_pth_at_explicit_purelib_root_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            purelib = Path(temporary)
            purelib.chmod(0o755)

            first = install_bootstrap(purelib)
            second = install_bootstrap(purelib)

            self.assertEqual(first, purelib / "python-udf-jit-bootstrap.pth")
            self.assertEqual(second, first)
            self.assertEqual(
                first.read_text(encoding="utf-8"),
                "import python_udf_jit.bootstrap; "
                "python_udf_jit.bootstrap.bootstrap_from_environment()\n",
            )
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)

    def test_rejects_symlink_and_group_writable_purelib_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "target"
            target.mkdir(mode=0o755)
            link = parent / "link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(
                BootstrapInstallError,
                "purelib_symlink",
            ):
                install_bootstrap(link)

            target.chmod(0o775)
            with self.assertRaisesRegex(
                BootstrapInstallError,
                "purelib_permissions",
            ):
                install_bootstrap(target)

    def test_rejects_existing_symlink_or_different_bootstrap_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            purelib = Path(temporary)
            purelib.chmod(0o755)
            destination = purelib / "python-udf-jit-bootstrap.pth"
            other = purelib / "other.pth"
            other.write_text("other\n", encoding="utf-8")
            destination.symlink_to(other)

            with self.assertRaisesRegex(
                BootstrapInstallError,
                "destination_symlink",
            ):
                install_bootstrap(purelib)

            destination.unlink()
            destination.write_text("different\n", encoding="utf-8")
            os.chmod(destination, 0o644)
            with self.assertRaisesRegex(
                BootstrapInstallError,
                "destination_conflict",
            ):
                install_bootstrap(purelib)


if __name__ == "__main__":
    unittest.main()
