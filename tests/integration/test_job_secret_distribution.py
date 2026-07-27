from __future__ import annotations

import json
import multiprocessing
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from pathlib import Path

from python_udf_jit.governance.credentials import (
    CredentialDistributionSnapshot,
    CredentialError,
    CredentialScope,
    CredentialVault,
)


ROOT = Path(__file__).resolve().parents[2]


def _credential_worker(
    connection: Connection,
    job_id: str,
    generation: int,
) -> None:
    material = bytearray(connection.recv_bytes())
    scope = CredentialScope(job_id=job_id, trust_domain="same_job")
    with CredentialVault() as vault:
        handle = vault.issue(
            material,
            scope=scope,
            generation=generation,
        )
        usable = vault.use(
            handle,
            scope=scope,
            consumer=lambda view: len(view) == 32,
        )
        try:
            vault.use(
                handle,
                scope=CredentialScope(
                    job_id=f"{job_id}-other",
                    trust_domain="same_job",
                ),
                consumer=lambda _view: True,
            )
        except CredentialError:
            cross_job_denied = True
        else:
            cross_job_denied = False
        response = {
            "usable": usable,
            "source_zeroized": not any(material),
            "cross_job_denied": cross_job_denied,
            "evidence_scope": "local_process_contract",
        }
    connection.send_bytes(
        json.dumps(response, sort_keys=True).encode("utf-8")
    )
    connection.close()


def _run_credential_worker(
    *,
    job_id: str,
    generation: int,
) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(
        target=_credential_worker,
        args=(child, job_id, generation),
    )
    process.start()
    child.close()
    parent.send_bytes(os.urandom(32))
    if not parent.poll(10):
        process.terminate()
        process.join(10)
        raise AssertionError("credential worker response timeout")
    response = json.loads(parent.recv_bytes().decode("utf-8"))
    parent.close()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise AssertionError("credential worker exit timeout")
    if process.exitcode != 0:
        raise AssertionError(f"credential worker exit={process.exitcode}")
    return response


class JobSecretDistributionIntegrationTests(unittest.TestCase):
    def test_two_workers_restart_and_two_jobs_remain_isolated(self) -> None:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = (
                executor.submit(
                    _run_credential_worker,
                    job_id="job-a",
                    generation=1,
                ),
                executor.submit(
                    _run_credential_worker,
                    job_id="job-a",
                    generation=1,
                ),
                executor.submit(
                    _run_credential_worker,
                    job_id="job-b",
                    generation=1,
                ),
            )
            first, second, other_job = (
                future.result(timeout=15) for future in futures
            )
        restarted = _run_credential_worker(job_id="job-a", generation=2)

        for result in (first, second, restarted, other_job):
            self.assertTrue(result["usable"])
            self.assertTrue(result["source_zeroized"])
            self.assertTrue(result["cross_job_denied"])
            self.assertEqual(
                result["evidence_scope"],
                "local_process_contract",
            )

    def test_rotation_grace_revocation_and_channel_expiry_close_optimization(
        self,
    ) -> None:
        window = CredentialDistributionSnapshot(
            job_id="job-a",
            tenant_id="tenant-a",
            key_id="key-a",
            algorithm="hmac-sha256",
            active_generation=2,
            previous_generation=1,
            grace_expires_at_ns=100,
            revoked_through=0,
            issued_at_ns=50,
            active_expires_at_ns=180,
            channel_expires_at_ns=200,
        )

        self.assertTrue(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=1,
                now_ns=100,
            ).optimized_execution_allowed
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=1,
                now_ns=101,
            ).reason,
            "rotation_grace_expired",
        )
        self.assertEqual(
            window.admit(
                job_id="job-b",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=2,
                now_ns=100,
            ).reason,
            "job_scope_mismatch",
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-b",
                key_id="key-a",
                generation=2,
                now_ns=100,
            ).reason,
            "tenant_scope_mismatch",
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-b",
                generation=2,
                now_ns=100,
            ).reason,
            "key_id_mismatch",
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=3,
                now_ns=100,
            ).reason,
            "generation_not_admitted",
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=2,
                now_ns=49,
            ).reason,
            "credential_not_yet_valid",
        )
        self.assertEqual(
            window.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=2,
                now_ns=181,
            ).reason,
            "credential_expired",
        )
        revoked = CredentialDistributionSnapshot(
            job_id="job-a",
            tenant_id="tenant-a",
            key_id="key-a",
            algorithm="hmac-sha256",
            active_generation=2,
            previous_generation=1,
            grace_expires_at_ns=150,
            revoked_through=1,
            issued_at_ns=50,
            active_expires_at_ns=180,
            channel_expires_at_ns=200,
        )
        self.assertEqual(
            revoked.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=1,
                now_ns=120,
            ).reason,
            "generation_revoked",
        )
        self.assertEqual(
            revoked.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=2,
                now_ns=201,
            ).reason,
            "credential_channel_expired",
        )
        fully_revoked = CredentialDistributionSnapshot(
            job_id="job-a",
            tenant_id="tenant-a",
            key_id="key-a",
            algorithm="hmac-sha256",
            active_generation=2,
            previous_generation=1,
            grace_expires_at_ns=150,
            revoked_through=2,
            issued_at_ns=50,
            active_expires_at_ns=180,
            channel_expires_at_ns=200,
        )
        self.assertEqual(
            fully_revoked.admit(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                generation=2,
                now_ns=120,
            ).reason,
            "generation_revoked",
        )
        with self.assertRaisesRegex(
            CredentialError,
            "credential_algorithm_invalid",
        ):
            CredentialDistributionSnapshot(
                job_id="job-a",
                tenant_id="tenant-a",
                key_id="key-a",
                algorithm="sha256",
                active_generation=2,
                previous_generation=1,
                grace_expires_at_ns=150,
                revoked_through=0,
                issued_at_ns=50,
                active_expires_at_ns=180,
                channel_expires_at_ns=200,
            )

    def test_local_process_contract_is_not_external_platform_evidence(self) -> None:
        matrix = json.loads(
            (ROOT / "config/mainline-support-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        prerequisite = matrix["release_prerequisites"][
            "credential_distribution"
        ]

        self.assertEqual(prerequisite["status"], "incomplete")
        self.assertEqual(prerequisite["gate_outcome"], "stop")
        self.assertEqual(
            prerequisite["local_evidence_scope"],
            "local_process_contract",
        )
        self.assertIsNone(prerequisite["external_evidence"])


if __name__ == "__main__":
    unittest.main()
