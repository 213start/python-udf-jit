from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.protocol.admission import (
    ComponentCapabilities,
    admit_driver_worker,
)


class ComponentAdmissionIntegrationTest(unittest.TestCase):
    def test_driver_worker_mismatch_rejects_before_resource_qualification(
        self,
    ):
        current = ComponentCapabilities.current()
        incompatible = {
            "artifact-format": dataclasses.replace(
                current,
                artifact_format_minor=1,
            ),
            "runtime-abi": dataclasses.replace(
                current,
                runtime_abi=2,
            ),
            "adapter-abi": dataclasses.replace(
                current,
                adapter_abi=2,
            ),
        }

        self.assertTrue(
            admit_driver_worker(current, current).accepted
        )
        self.assertTrue(
            all(
                not admit_driver_worker(
                    current,
                    worker,
                ).accepted
                for worker in incompatible.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
