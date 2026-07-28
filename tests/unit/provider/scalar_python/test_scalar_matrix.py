from __future__ import annotations

import math
import hashlib
import unittest

from python_udf_jit.compiler.capture import FallbackIdentity
from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    LogicalType,
    Nullability,
    SemanticBlock,
    SemanticControlEdge,
    SemanticLiteral,
    SemanticOperation,
    build_semantic_module,
)
from python_udf_jit.compiler.region import form_semantic_region_graph
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import (
    decode_artifact,
    encode_artifact,
)
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
    BOOL_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
    LocalScalarSlotBackend,
)


def _attributes(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _operation(
    operation_id: str,
    block_id: str,
    op: str,
    operands: tuple[str, ...],
    result_id: str | None,
    result_type: LogicalType,
    nullability: Nullability,
    *,
    attributes: tuple[tuple[str, str], ...] = (),
    literal: SemanticLiteral | None = None,
) -> SemanticOperation:
    return SemanticOperation(
        operation_id,
        block_id,
        op,
        operands,
        result_id,
        result_type,
        nullability,
        EffectKind.PURE,
        False,
        None,
        Determinism.DETERMINISTIC,
        attributes,
        literal,
    )


def _identity_module(
    logical_type: LogicalType,
    *,
    nullable: bool,
):
    nullability = (
        Nullability.NULLABLE
        if nullable
        else Nullability.NON_NULL
    )
    operations = (
        _operation(
            "op0",
            "b0",
            "argument",
            (),
            "%0",
            logical_type,
            nullability,
            attributes=_attributes(index="0"),
        ),
        _operation(
            "op1",
            "b0",
            "return",
            ("%0",),
            None,
            logical_type,
            nullability,
        ),
    )
    return build_semantic_module(
        function_id=_hash(
            f"identity-{logical_type.value}-{nullable}"
        ),
        entry_block="b0",
        input_types=(logical_type,),
        input_nullability=(nullability,),
        output_type=logical_type,
        output_nullability=nullability,
        blocks=(SemanticBlock("b0", ("op0", "op1")),),
        control_edges=(),
        operations=operations,
        return_operation_id="op1",
    )


def _branch_module():
    operations = (
        _operation(
            "op0",
            "b0",
            "argument",
            (),
            "%0",
            LogicalType.INT64,
            Nullability.NON_NULL,
            attributes=_attributes(index="0"),
        ),
        _operation(
            "op1",
            "b0",
            "constant",
            (),
            "%1",
            LogicalType.INT64,
            Nullability.NON_NULL,
            literal=SemanticLiteral.from_value(0),
        ),
        _operation(
            "op2",
            "b0",
            "compare.ge",
            ("%0", "%1"),
            "%2",
            LogicalType.BOOL,
            Nullability.NON_NULL,
        ),
        _operation(
            "op3",
            "b0",
            "binary.sub",
            ("%1", "%0"),
            "%3",
            LogicalType.INT64,
            Nullability.NON_NULL,
        ),
        _operation(
            "op4",
            "b0",
            "select",
            ("%2", "%0", "%3"),
            "%4",
            LogicalType.INT64,
            Nullability.NON_NULL,
        ),
        _operation(
            "op5",
            "b0",
            "branch",
            ("%2",),
            None,
            LogicalType.BOOL,
            Nullability.NON_NULL,
            attributes=_attributes(
                false_block="b2",
                true_block="b1",
            ),
        ),
        _operation(
            "op6",
            "b1",
            "jump",
            (),
            None,
            LogicalType.INT64,
            Nullability.NON_NULL,
            attributes=_attributes(target_block="b3"),
        ),
        _operation(
            "op7",
            "b2",
            "jump",
            (),
            None,
            LogicalType.INT64,
            Nullability.NON_NULL,
            attributes=_attributes(target_block="b3"),
        ),
        _operation(
            "op8",
            "b3",
            "return",
            ("%4",),
            None,
            LogicalType.INT64,
            Nullability.NON_NULL,
        ),
    )
    return build_semantic_module(
        function_id=_hash("branch-absolute-value"),
        entry_block="b0",
        input_types=(LogicalType.INT64,),
        input_nullability=(Nullability.NON_NULL,),
        output_type=LogicalType.INT64,
        output_nullability=Nullability.NON_NULL,
        blocks=(
            SemanticBlock(
                "b0",
                ("op0", "op1", "op2", "op3", "op4", "op5"),
            ),
            SemanticBlock("b1", ("op6",)),
            SemanticBlock("b2", ("op7",)),
            SemanticBlock("b3", ("op8",)),
        ),
        control_edges=(
            SemanticControlEdge("b0", "b1", "branch_true"),
            SemanticControlEdge("b0", "b2", "branch_false"),
            SemanticControlEdge("b1", "b3", "jump"),
            SemanticControlEdge("b2", "b3", "jump"),
        ),
        operations=operations,
        return_operation_id="op8",
    )


_LOGICAL_TYPES = {
    BOOL_SCALAR_TYPE: LogicalType.BOOL,
    INT32_SCALAR_TYPE: LogicalType.INT64,
    INT64_SCALAR_TYPE: LogicalType.INT64,
    FLOAT32_SCALAR_TYPE: LogicalType.FLOAT64,
    FLOAT64_SCALAR_TYPE: LogicalType.FLOAT64,
}


class ScalarTypeMatrixTest(unittest.TestCase):
    def _compile(
        self,
        scalar_type: str,
        *,
        nullable: bool,
        module=None,
    ):
        semantic_module = module or _identity_module(
            _LOGICAL_TYPES[scalar_type],
            nullable=nullable,
        )
        graph = form_semantic_region_graph(semantic_module)
        input_spec = scalar_input_spec(
            scalar_type,
            nullable=nullable,
        )
        output_spec = scalar_output_spec(
            scalar_type,
            nullable=nullable,
        )
        registry = CapabilityRegistry(epoch="epoch-matrix")
        input_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type=scalar_type,
                nullable=nullable,
            )
        )
        output_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type=scalar_type,
                nullable=nullable,
            )
        )
        compiled = compile_semantic_scalar_region(
            semantic_module,
            graph,
            input_spec=input_spec,
            output_spec=output_spec,
            registry=registry,
        )
        return registry, input_handle, output_handle, compiled

    def _assert_value_equal(self, actual, expected) -> None:
        if type(expected) is float:
            if math.isnan(expected):
                self.assertTrue(math.isnan(actual))
            else:
                self.assertEqual(actual.hex(), expected.hex())
        else:
            self.assertEqual(actual, expected)
            self.assertIs(type(actual), type(expected))

    def test_five_scalar_types_execute_through_typed_input_and_output_slots(
        self,
    ) -> None:
        cases = {
            BOOL_SCALAR_TYPE: (False, True),
            INT32_SCALAR_TYPE: (-(1 << 31), 0, (1 << 31) - 1),
            INT64_SCALAR_TYPE: (-(1 << 63), 0, (1 << 63) - 1),
            FLOAT32_SCALAR_TYPE: (
                0.0,
                -0.0,
                1.1,
                float("inf"),
                float("nan"),
            ),
            FLOAT64_SCALAR_TYPE: (
                0.0,
                -0.0,
                1.1,
                float("inf"),
                float("nan"),
            ),
        }
        code_hashes = set()
        for scalar_type, values in cases.items():
            with self.subTest(scalar_type=scalar_type):
                registry, input_handle, output_handle, compiled = (
                    self._compile(
                        scalar_type,
                        nullable=False,
                    )
                )
                code_hashes.add(compiled.code_hash)
                executor = ScalarExecutor(registry)
                try:
                    for value in values:
                        actual = executor.execute(
                            compiled,
                            input_handle,
                            output_handle,
                            value,
                        )
                        expected = LocalScalarSlotBackend(
                            scalar_type=scalar_type,
                            nullable=False,
                        )
                        expected.begin_borrow()
                        expected.write_scalar(
                            value,
                            scalar_type=scalar_type,
                            nullable=False,
                        )
                        normalized = expected.load_scalar(
                            scalar_type=scalar_type,
                            nullable=False,
                        )
                        expected.end_borrow()
                        expected.close()
                        self._assert_value_equal(actual, normalized)
                finally:
                    registry.release(output_handle)
                    registry.release(input_handle)
        self.assertEqual(len(code_hashes), 5)

    def test_five_scalar_types_roundtrip_through_formal_artifact_layout(
        self,
    ) -> None:
        for scalar_type, logical_type in _LOGICAL_TYPES.items():
            for nullable in (False, True):
                with self.subTest(
                    scalar_type=scalar_type,
                    nullable=nullable,
                ):
                    module = _identity_module(
                        logical_type,
                        nullable=nullable,
                    )
                    artifact = build_artifact(
                        module,
                        form_semantic_region_graph(module),
                        FallbackIdentity(
                            "tests.scalar_matrix",
                            "identity",
                            module.function_id,
                        ),
                        input_access_specs=(
                            scalar_input_spec(
                                scalar_type,
                                nullable=nullable,
                            ),
                        ),
                        output_access_spec=scalar_output_spec(
                            scalar_type,
                            nullable=nullable,
                        ),
                    )
                    restored = decode_artifact(
                        encode_artifact(artifact)
                    )
                    self.assertEqual(
                        restored.input_access_specs,
                        artifact.input_access_specs,
                    )
                    self.assertEqual(
                        restored.output_access_spec,
                        artifact.output_access_spec,
                    )
                    self.assertEqual(
                        restored.guard_template["input_types"],
                        [scalar_type],
                    )
                    self.assertEqual(
                        restored.guard_template["output_type"],
                        scalar_type,
                    )

    def test_nullable_matrix_preserves_null_without_loading_a_value(
        self,
    ) -> None:
        representatives = {
            BOOL_SCALAR_TYPE: True,
            INT32_SCALAR_TYPE: 7,
            INT64_SCALAR_TYPE: 1 << 40,
            FLOAT32_SCALAR_TYPE: 1.25,
            FLOAT64_SCALAR_TYPE: -3.5,
        }
        for scalar_type, value in representatives.items():
            with self.subTest(scalar_type=scalar_type):
                registry, input_handle, output_handle, compiled = (
                    self._compile(
                        scalar_type,
                        nullable=True,
                    )
                )
                executor = ScalarExecutor(registry)
                try:
                    self.assertIsNone(
                        executor.execute(
                            compiled,
                            input_handle,
                            output_handle,
                            None,
                        )
                    )
                    self._assert_value_equal(
                        executor.execute(
                            compiled,
                            input_handle,
                            output_handle,
                            value,
                        ),
                        value,
                    )
                finally:
                    registry.release(output_handle)
                    registry.release(input_handle)

    def test_local_branch_and_comparison_publish_exactly_one_result(
        self,
    ) -> None:
        registry, input_handle, output_handle, compiled = self._compile(
            INT64_SCALAR_TYPE,
            nullable=False,
            module=_branch_module(),
        )
        executor = ScalarExecutor(registry)
        try:
            self.assertEqual(
                executor.execute(
                    compiled,
                    input_handle,
                    output_handle,
                    -9,
                ),
                9,
            )
            self.assertEqual(
                executor.execute(
                    compiled,
                    input_handle,
                    output_handle,
                    7,
                ),
                7,
            )
        finally:
            registry.release(output_handle)
            registry.release(input_handle)

    def test_layout_and_semantic_type_mismatch_is_rejected_before_compile(
        self,
    ) -> None:
        module = _identity_module(
            LogicalType.INT64,
            nullable=False,
        )
        with self.assertRaisesRegex(
            ValueError,
            "semantic input type",
        ):
            compile_semantic_scalar_region(
                module,
                form_semantic_region_graph(module),
                input_spec=scalar_input_spec(
                    FLOAT64_SCALAR_TYPE,
                    nullable=False,
                ),
                output_spec=scalar_output_spec(
                    INT64_SCALAR_TYPE,
                    nullable=False,
                ),
                registry=CapabilityRegistry(epoch="epoch-mismatch"),
            )


if __name__ == "__main__":
    unittest.main()
