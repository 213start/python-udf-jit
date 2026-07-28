from __future__ import annotations

import unittest

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.region import form_semantic_region_graph
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import decode_artifact, encode_artifact
from python_udf_jit.provider.scalar_python.capability import (
    CapabilityRegistry,
)
from python_udf_jit.provider.scalar_python.compiler import (
    compile_semantic_scalar_region,
)
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.descriptors import (
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import (
    LocalScalarSlotBackend,
    normalize_scalar_value,
)
from tests.unit.provider.scalar_python.test_scalar_matrix import (
    _LOGICAL_TYPES,
    _branch_module,
    _identity_module,
)


class RFC006UnitTests(unittest.TestCase):
    def test_rfc006_unit_contract(self) -> None:
        representatives = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": 1 << 40,
            "float32": 1.1,
            "float64": -3.5,
        }
        code_hashes = set()
        for scalar_type, value in representatives.items():
            with self.subTest(scalar_type=scalar_type):
                module = _identity_module(
                    _LOGICAL_TYPES[scalar_type],
                    nullable=True,
                )
                graph = form_semantic_region_graph(module)
                input_spec = scalar_input_spec(
                    scalar_type,
                    nullable=True,
                )
                output_spec = scalar_output_spec(
                    scalar_type,
                    nullable=True,
                )
                artifact = decode_artifact(
                    encode_artifact(
                        build_artifact(
                            module,
                            graph,
                            FallbackIdentity(
                                "tests.rfc006",
                                f"identity_{scalar_type}",
                                module.function_id,
                            ),
                            input_access_specs=(input_spec,),
                            output_access_spec=output_spec,
                        )
                    )
                )
                self.assertEqual(
                    artifact.input_access_specs,
                    (input_spec,),
                )
                self.assertEqual(
                    artifact.output_access_spec,
                    output_spec,
                )

                registry = CapabilityRegistry(epoch="rfc006-unit")
                input_handle = registry.register(
                    LocalScalarSlotBackend(
                        scalar_type=scalar_type,
                        nullable=True,
                    )
                )
                output_handle = registry.register(
                    LocalScalarSlotBackend(
                        scalar_type=scalar_type,
                        nullable=True,
                    )
                )
                compiled = compile_semantic_scalar_region(
                    artifact.semantic_core_module,
                    artifact.semantic_region_graph,
                    input_spec=input_spec,
                    output_spec=output_spec,
                    registry=registry,
                )
                code_hashes.add(compiled.code_hash)
                executor = ScalarExecutor(registry)
                try:
                    actual = executor.execute(
                        compiled,
                        input_handle,
                        output_handle,
                        value,
                    )
                    expected = normalize_scalar_value(
                        value,
                        scalar_type,
                        nullable=True,
                    )
                    if isinstance(expected, float):
                        self.assertEqual(actual.hex(), expected.hex())
                    else:
                        self.assertEqual(actual, expected)
                        self.assertIs(type(actual), type(expected))
                    self.assertIsNone(
                        executor.execute(
                            compiled,
                            input_handle,
                            output_handle,
                            None,
                        )
                    )
                finally:
                    registry.release(output_handle)
                    registry.release(input_handle)

        self.assertEqual(len(code_hashes), 5)

        module = _branch_module()
        graph = form_semantic_region_graph(module)
        input_spec = scalar_input_spec("int64", nullable=False)
        output_spec = scalar_output_spec("int64", nullable=False)
        registry = CapabilityRegistry(epoch="rfc006-branch")
        input_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type="int64",
                nullable=False,
            )
        )
        output_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type="int64",
                nullable=False,
            )
        )
        compiled = compile_semantic_scalar_region(
            module,
            graph,
            input_spec=input_spec,
            output_spec=output_spec,
            registry=registry,
        )
        executor = ScalarExecutor(registry)
        try:
            self.assertEqual(
                [
                    executor.execute(
                        compiled,
                        input_handle,
                        output_handle,
                        value,
                    )
                    for value in (-9, 7)
                ],
                [9, 7],
            )
        finally:
            registry.release(output_handle)
            registry.release(input_handle)


if __name__ == "__main__":
    unittest.main()
