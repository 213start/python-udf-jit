from __future__ import annotations

import json
import pickle
import unittest

from python_udf_jit.integration.daft_ray.carrier import (
    CarrierContractError,
    ExecutionCarrierObservation,
    ProductionCarrierState,
    validate_execution_carrier,
)


class DriverWorkerCarrierProbeTests(unittest.TestCase):
    def test_placeholder_state_hash_is_deterministic_and_serializable(self) -> None:
        state = ProductionCarrierState.placeholder(
            candidate_id="calibration",
            manifest_sha256="a" * 64,
        )
        same = ProductionCarrierState.placeholder(
            candidate_id="calibration",
            manifest_sha256="a" * 64,
        )

        self.assertEqual(state.state_sha256, same.state_sha256)
        self.assertEqual(state, ProductionCarrierState.from_bytes(state.to_bytes()))
        self.assertEqual(state, pickle.loads(pickle.dumps(state)))
        self.assertFalse(state.finalized)
        self.assertEqual("placeholder", state.handle.kind)
        document = json.loads(state.to_bytes())
        cases = {}
        for name, value in (
            ("schema_version", "1"),
            ("candidate_id", 1),
        ):
            changed = dict(document)
            changed[name] = value
            cases[name] = changed
        changed_handle = dict(document)
        changed_handle["handle"] = {
            **document["handle"],
            "size_bytes": False,
        }
        cases["handle_size_bytes"] = changed_handle
        changed_extra = dict(document)
        changed_extra["future_field"] = None
        cases["unknown_field"] = changed_extra

        for name, changed in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(CarrierContractError):
                    ProductionCarrierState.from_bytes(
                        json.dumps(changed).encode("ascii")
                    )

    def test_finalize_changes_hash_and_is_idempotent_for_same_artifact(self) -> None:
        state = ProductionCarrierState.placeholder("calibration", "a" * 64)

        finalized = state.finalize(b"portable-artifact")
        finalized_again = finalized.finalize(b"portable-artifact")

        self.assertNotEqual(state.state_sha256, finalized.state_sha256)
        self.assertEqual(finalized, finalized_again)
        self.assertTrue(finalized.finalized)
        self.assertEqual(b"portable-artifact", finalized.artifact_bytes)
        self.assertEqual(finalized, ProductionCarrierState.from_bytes(finalized.to_bytes()))
        with self.assertRaisesRegex(CarrierContractError, "different artifact"):
            finalized.finalize(b"different")

    def test_real_carrier_must_request_cpu_and_run_on_a_worker(self) -> None:
        observation = ExecutionCarrierObservation(
            carrier_kind="SwordfishActor",
            actor_or_worker_id="actor-1",
            node_id="worker-1-id",
            pid=321,
            process_generation="boot-1:321:1",
            required_cpus=1.0,
        )
        validate_execution_carrier(observation, {"worker-1-id", "worker-2-id"})

        with self.assertRaisesRegex(CarrierContractError, "logical CPU"):
            validate_execution_carrier(
                ExecutionCarrierObservation(
                    "SwordfishActor", "actor-2", "worker-2-id", 322, "g2", 0.0
                ),
                {"worker-1-id", "worker-2-id"},
            )
        with self.assertRaisesRegex(CarrierContractError, "Worker node"):
            validate_execution_carrier(
                ExecutionCarrierObservation(
                    "SwordfishActor", "actor-3", "head-id", 323, "g3", 1.0
                ),
                {"worker-1-id", "worker-2-id"},
            )


if __name__ == "__main__":
    unittest.main()
