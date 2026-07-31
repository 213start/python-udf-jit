from __future__ import annotations

import dis
import math
import unittest
from unittest.mock import patch

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.bytecode_decoder import decode_code
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.diagnostics.provenance import UpperProvenanceRecorder
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import (
    ScalarLoweringHooks,
    compile_semantic_scalar_region,
    compile_scalar_region,
)
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.descriptors import (
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import FLOAT64_SCALAR_TYPE
from python_udf_jit.runtime.layout import LocalScalarSlotBackend


def affine(x):
    return x * 2.0 + 3.0


def shifted_product(x):
    return x * 4.0 - 1.5


def changed_constant(x):
    return x * 2.0 + 4.0


def changed_operator(x):
    return x * 2.0 - 3.0


def diagnostic_secret_constant(x):
    return x + 1234567.125


class CompilerTemplateTest(unittest.TestCase):
    def compile(self, function):
        module = lower_capture(capture(CaptureRequest(function)))
        region = form_verified_region(module)
        registry = CapabilityRegistry(epoch="epoch-a")
        compiled = compile_scalar_region(module, region, registry=registry)
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        return (
            module,
            registry,
            compiled,
            input_handle,
            output_handle,
        )

    def test_interpreter_template_is_driven_by_verified_region(self):
        module, registry, compiled, input_handle, output_handle = self.compile(
            affine
        )
        executor = ScalarExecutor(registry)

        for value in (0.0, -0.0, 1.25, float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                actual = executor.execute(
                    compiled,
                    input_handle,
                    output_handle,
                    value,
                )
                expected = affine(value)
                if math.isnan(expected):
                    self.assertTrue(math.isnan(actual))
                else:
                    self.assertEqual(actual.hex(), expected.hex())

        self.assertEqual(compiled.semantic_hash, module.semantic_hash)
        self.assertEqual(compiled.execution_mode, "python-interpreter")
        names = compiled.code_object.co_names
        self.assertIn("_udf_guard_data_handle", names)
        self.assertIn("_udf_data_load", names)
        self.assertIn("_udf_data_store", names)
        self.assertIn("BINARY_OP", {instruction.opname for instruction in dis.get_instructions(compiled.code_object)})
        registry.release(output_handle)
        registry.release(input_handle)

    def test_guard_completes_before_data_load_is_called(self):
        module = lower_capture(capture(CaptureRequest(affine)))
        region = form_verified_region(module)
        calls = []

        def guard(handle):
            calls.append(("guard", handle))
            return "guarded"

        def load(guarded):
            calls.append(("load", guarded))
            return 2.0

        def store(guarded, value):
            calls.append(("store", guarded, value))
            return value

        compiled = compile_scalar_region(
            module,
            region,
            hooks=ScalarLoweringHooks(
                guard,
                lambda _guarded: False,
                load,
                store,
                lambda _guarded: None,
            ),
        )

        self.assertEqual(compiled("input", "output"), 7.0)
        self.assertEqual(
            calls,
            [
                ("guard", "input"),
                ("load", "guarded"),
                ("guard", "output"),
                ("store", "guarded", 7.0),
            ],
        )

    def test_each_constant_or_operator_change_changes_code_hash_and_result(self):
        _, registry_a, compiled_a, input_a, output_a = self.compile(affine)
        _, registry_b, compiled_b, input_b, output_b = self.compile(
            changed_constant
        )
        _, registry_c, compiled_c, input_c, output_c = self.compile(
            changed_operator
        )

        result_a = ScalarExecutor(registry_a).execute(
            compiled_a, input_a, output_a, 2.0
        )
        result_b = ScalarExecutor(registry_b).execute(
            compiled_b, input_b, output_b, 2.0
        )
        result_c = ScalarExecutor(registry_c).execute(
            compiled_c, input_c, output_c, 2.0
        )

        self.assertNotEqual(compiled_a.code_hash, compiled_b.code_hash)
        self.assertNotEqual(compiled_a.code_hash, compiled_c.code_hash)
        self.assertEqual(result_a, 7.0)
        self.assertEqual(result_b, 8.0)
        self.assertEqual(result_c, 1.0)
        registry_a.release(output_a)
        registry_a.release(input_a)
        registry_b.release(output_b)
        registry_b.release(input_b)
        registry_c.release(output_c)
        registry_c.release(input_c)

    def test_explicit_provenance_sink_maps_generated_offsets_to_operations(self):
        result = compile_semantic(capture(CaptureRequest(affine)))
        self.assertTrue(result.accepted)
        assert result.core_module is not None
        assert result.region_graph is not None
        recorder = UpperProvenanceRecorder(
            decode_code(affine.__code__).source_map,
            result.core_module,
            result.region_graph,
        )
        registry = CapabilityRegistry(epoch="epoch-provenance")

        compiled = compile_semantic_scalar_region(
            result.core_module,
            result.region_graph,
            input_spec=scalar_input_spec(
                FLOAT64_SCALAR_TYPE,
                nullable=False,
            ),
            output_spec=scalar_output_spec(
                FLOAT64_SCALAR_TYPE,
                nullable=False,
            ),
            registry=registry,
            provenance_sink=recorder,
        )

        generated_nodes = {
            node.bytecode_offset: node
            for node in recorder.provenance_map.nodes
            if node.layer.value == "generated_bytecode"
        }
        lowered_operations = {
            edge.from_node_id
            for edge in recorder.provenance_map.edges
            if edge.to_node_id in {
                node.node_id for node in generated_nodes.values()
            }
            and edge.from_node_id.startswith("core:")
        }
        expected_operations = {
            f"core:{result.core_module.semantic_hash}:{operation.operation_id}"
            for operation in result.core_module.operations
        }
        source_mapped_operations = {
            f"core:{result.core_module.semantic_hash}:{operation.operation_id}"
            for operation in result.core_module.operations
            if operation.source_offset is not None
        }
        self.assertEqual(lowered_operations, expected_operations)
        self.assertEqual(
            set(generated_nodes),
            {
                instruction.offset
                for instruction in dis.get_instructions(
                    compiled.code_object,
                    show_caches=True,
                )
            },
        )
        for operation_id in expected_operations:
            generated = {
                node.node_id
                for node in recorder.provenance_map.trace_downstream(
                    operation_id
                )
                if node.layer.value == "generated_bytecode"
            }
            self.assertTrue(generated)
            if operation_id in source_mapped_operations:
                self.assertTrue(
                    any(
                        node.layer.value == "source"
                        for generated_id in generated
                        for node in recorder.provenance_map.trace_upstream(
                            generated_id
                        )
                    )
                )
        self.assertIn("FunctionDef", recorder.generated_ast_text)
        self.assertTrue(recorder.lowering_map["entries"])

    def test_generated_ast_diagnostic_redacts_literal_bodies(self):
        result = compile_semantic(
            capture(CaptureRequest(diagnostic_secret_constant))
        )
        assert result.core_module is not None
        assert result.region_graph is not None
        recorder = UpperProvenanceRecorder(
            decode_code(diagnostic_secret_constant.__code__).source_map,
            result.core_module,
            result.region_graph,
        )

        compile_semantic_scalar_region(
            result.core_module,
            result.region_graph,
            input_spec=scalar_input_spec(
                FLOAT64_SCALAR_TYPE,
                nullable=False,
            ),
            output_spec=scalar_output_spec(
                FLOAT64_SCALAR_TYPE,
                nullable=False,
            ),
            registry=CapabilityRegistry(epoch="epoch-redacted-ast"),
            provenance_sink=recorder,
        )

        self.assertNotIn("1234567.125", recorder.generated_ast_text)
        self.assertIn("<redacted:float:", recorder.generated_ast_text)

    def test_no_provenance_sink_does_not_construct_snapshot(self):
        result = compile_semantic(capture(CaptureRequest(affine)))
        assert result.core_module is not None
        assert result.region_graph is not None
        registry = CapabilityRegistry(epoch="epoch-no-provenance")

        with patch(
            "python_udf_jit.provider.scalar_python.compiler."
            "ScalarLoweringSnapshot",
            side_effect=AssertionError("diagnostic snapshot constructed"),
        ):
            compile_semantic_scalar_region(
                result.core_module,
                result.region_graph,
                input_spec=scalar_input_spec(
                    FLOAT64_SCALAR_TYPE,
                    nullable=False,
                ),
                output_spec=scalar_output_spec(
                    FLOAT64_SCALAR_TYPE,
                    nullable=False,
                ),
                registry=registry,
            )


if __name__ == "__main__":
    unittest.main()
