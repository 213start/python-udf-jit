from __future__ import annotations

import copy
import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.governance.credentials import (
    CredentialError,
    CredentialScope,
    CredentialVault,
)


class CredentialVaultTests(unittest.TestCase):
    def test_handle_is_opaque_nonserializable_and_never_contains_secret(self) -> None:
        secret = bytearray(b"mainline-job-secret")
        scope = CredentialScope(job_id="job-1", trust_domain="tenant-a")
        vault = CredentialVault()

        handle = vault.issue(secret, scope=scope, generation=1)

        self.assertEqual(secret, bytearray(len(secret)))
        rendered = repr(handle)
        self.assertNotIn("mainline-job-secret", rendered)
        self.assertFalse(hasattr(handle, "__dict__"))
        for operation in (
            lambda: pickle.dumps(handle),
            lambda: pickle.dumps(vault),
            lambda: copy.copy(handle),
            lambda: copy.deepcopy(handle),
            lambda: json.dumps(handle),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises((TypeError, ValueError)):
                    operation()

    def test_use_is_job_scoped_and_releases_the_transient_view(self) -> None:
        scope = CredentialScope(job_id="job-1", trust_domain="tenant-a")
        vault = CredentialVault()
        handle = vault.issue(b"secret", scope=scope, generation=1)
        retained_view = None

        def consume(view):
            nonlocal retained_view
            retained_view = view
            return bytes(view)

        self.assertEqual(vault.use(handle, scope=scope, consumer=consume), b"secret")
        assert retained_view is not None
        with self.assertRaises(ValueError):
            bytes(retained_view)
        with self.assertRaisesRegex(CredentialError, "scope_mismatch"):
            vault.use(
                handle,
                scope=CredentialScope("job-2", "tenant-a"),
                consumer=lambda view: bytes(view),
            )

    def test_revocation_generation_only_increases_and_cleanup_zeroizes(self) -> None:
        scope = CredentialScope("job-1", "tenant-a")
        vault = CredentialVault()
        first = vault.issue(b"first", scope=scope, generation=1)
        second = vault.issue(b"second", scope=scope, generation=2)

        vault.revoke_through(1)
        with self.assertRaisesRegex(CredentialError, "revoked"):
            vault.use(first, scope=scope, consumer=lambda view: bytes(view))
        self.assertEqual(
            vault.use(second, scope=scope, consumer=lambda view: bytes(view)),
            b"second",
        )
        with self.assertRaisesRegex(CredentialError, "revocation_generation"):
            vault.revoke_through(0)
        with self.assertRaisesRegex(CredentialError, "generation_revoked"):
            vault.issue(b"old", scope=scope, generation=1)

        vault.close()
        with self.assertRaisesRegex(CredentialError, "closed"):
            vault.use(second, scope=scope, consumer=lambda view: bytes(view))
        self.assertEqual(vault.debug_live_credential_count, 0)

    def test_vault_does_not_write_environment_argv_files_or_logs(self) -> None:
        before_environment = dict(os.environ)
        before_argv = tuple(sys.argv)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = CredentialVault()
            scope = CredentialScope("job-1", "tenant-a")
            handle = vault.issue(b"canary-secret", scope=scope, generation=1)
            vault.use(handle, scope=scope, consumer=lambda view: len(view))

            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(dict(os.environ), before_environment)
            self.assertEqual(tuple(sys.argv), before_argv)
            self.assertNotIn("canary-secret", repr(vault))


if __name__ == "__main__":
    unittest.main()
