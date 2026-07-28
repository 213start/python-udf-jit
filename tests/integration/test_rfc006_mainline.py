from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.region import form_semantic_region_graph
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact
from python_udf_jit.runtime.descriptors import (
    scalar_input_spec,
    scalar_output_spec,
)
from tests.integration.test_driver_worker_artifact_roundtrip import (
    worker_environment,
)
from tests.unit.provider.scalar_python.test_scalar_matrix import (
    _LOGICAL_TYPES,
    _identity_module,
)


class RFC006IntegrationTests(unittest.TestCase):
    def test_rfc006_integration_contract(self) -> None:
        representatives = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": 1 << 40,
            "float32": 1.1,
            "float64": -3.5,
        }
        payload = []
        for scalar_type, value in representatives.items():
            module = _identity_module(
                _LOGICAL_TYPES[scalar_type],
                nullable=True,
            )
            artifact = build_artifact(
                module,
                form_semantic_region_graph(module),
                FallbackIdentity(
                    "tests.rfc006.integration",
                    f"identity_{scalar_type}",
                    module.function_id,
                ),
                input_access_specs=(
                    scalar_input_spec(
                        scalar_type,
                        nullable=True,
                    ),
                ),
                output_access_spec=scalar_output_spec(
                    scalar_type,
                    nullable=True,
                ),
            )
            payload.append(
                {
                    "artifact": base64.b64encode(
                        encode_artifact(artifact)
                    ).decode("ascii"),
                    "scalar_type": scalar_type,
                    "value": value,
                }
            )

        script = """
import base64
import json
import os

from python_udf_jit.protocol.codec import decode_artifact
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import compile_semantic_scalar_region
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.layout import LocalScalarSlotBackend, normalize_scalar_value

reports = []
for case in json.loads(os.environ["UDFJIT_RFC006_CASES"]):
    artifact = decode_artifact(base64.b64decode(case["artifact"]))
    input_spec = artifact.input_access_specs[0]
    output_spec = artifact.output_access_spec
    registry = CapabilityRegistry(epoch="rfc006-worker")
    input_handle = registry.register(LocalScalarSlotBackend(
        scalar_type=input_spec.scalar_type,
        nullable=input_spec.nullable,
    ))
    output_handle = registry.register(LocalScalarSlotBackend(
        scalar_type=output_spec.scalar_type,
        nullable=output_spec.nullable,
    ))
    compiled = compile_semantic_scalar_region(
        artifact.semantic_core_module,
        artifact.semantic_region_graph,
        input_spec=input_spec,
        output_spec=output_spec,
        registry=registry,
    )
    executor = ScalarExecutor(registry)
    try:
        expected = normalize_scalar_value(
            case["value"],
            case["scalar_type"],
            nullable=True,
        )
        actual = executor.execute(
            compiled,
            input_handle,
            output_handle,
            case["value"],
        )
        if type(expected) is float:
            assert actual.hex() == expected.hex(), (actual, expected)
        else:
            assert actual == expected and type(actual) is type(expected)
        assert executor.execute(
            compiled,
            input_handle,
            output_handle,
            None,
        ) is None
        reports.append({
            "code_hash": compiled.code_hash,
            "scalar_type": case["scalar_type"],
        })
    finally:
        registry.release(output_handle)
        registry.release(input_handle)
print(json.dumps(reports, sort_keys=True, separators=(",", ":")))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=worker_environment(
                UDFJIT_RFC006_CASES=json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reports = json.loads(completed.stdout)
        self.assertEqual(
            {report["scalar_type"] for report in reports},
            set(representatives),
        )
        self.assertEqual(
            len({report["code_hash"] for report in reports}),
            5,
        )


if __name__ == "__main__":
    unittest.main()
