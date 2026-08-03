from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    SemanticLiteral,
)
from python_udf_jit.compiler.typed_ir import (
    BOOL,
    EXACT_UNICODE,
    INT64,
    UNICODE_SCALAR,
    Exactness,
    TypeKind,
    TypeSpec,
    TypedBlock,
    TypedBlockArgument,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
    build_typed_module,
    rehash_typed_module,
)
from python_udf_jit.compiler.typed_reference import (
    execute_typed_module,
)
from python_udf_jit.compiler.typed_verifier import (
    TypedVerificationError,
    verify_typed_module,
)


def _op(
    operation_id: str,
    block_id: str,
    op: str,
    operands: tuple[str, ...] = (),
    *,
    result_id: str | None = None,
    result_type: TypeSpec | None = None,
    attributes: tuple[tuple[str, str], ...] = (),
    literal: SemanticLiteral | None = None,
    may_raise: bool = False,
    exception_order: int | None = None,
) -> TypedOperation:
    return TypedOperation(
        operation_id=operation_id,
        block_id=block_id,
        op=op,
        operands=operands,
        result_id=result_id,
        result_type=result_type,
        effect=EffectKind.PURE,
        may_raise=may_raise,
        exception_order=exception_order,
        determinism=Determinism.DETERMINISTIC,
        attributes=attributes,
        literal=literal,
    )


def _unicode_count_module() -> TypedSemanticModule:
    operations = (
        _op(
            "op0",
            "entry",
            "argument",
            result_id="%text",
            result_type=EXACT_UNICODE,
            attributes=(("index", "0"),),
        ),
        _op(
            "op1",
            "entry",
            "constant",
            result_id="%zero",
            result_type=INT64,
            literal=SemanticLiteral.from_value(0),
        ),
        _op(
            "op2",
            "entry",
            "constant",
            result_id="%one",
            result_type=INT64,
            literal=SemanticLiteral.from_value(1),
        ),
        _op(
            "op3",
            "entry",
            "sequence.length",
            ("%text",),
            result_id="%length",
            result_type=INT64,
        ),
        _op(
            "op4",
            "entry",
            "jump",
            attributes=(("target_block", "header"),),
        ),
        _op(
            "op5",
            "header",
            "compare.lt",
            ("%index", "%length"),
            result_id="%continue",
            result_type=BOOL,
        ),
        _op(
            "op6",
            "header",
            "branch",
            ("%continue",),
            attributes=(
                ("false_block", "exit"),
                ("true_block", "body"),
            ),
        ),
        _op(
            "op7",
            "body",
            "sequence.get",
            ("%text", "%index"),
            result_id="%character",
            result_type=UNICODE_SCALAR,
            may_raise=True,
            exception_order=0,
        ),
        _op(
            "op8",
            "body",
            "unicode.property",
            ("%character",),
            result_id="%matches",
            result_type=BOOL,
            attributes=(("property", "alnum"),),
        ),
        _op(
            "op9",
            "body",
            "cast",
            ("%matches",),
            result_id="%increment",
            result_type=INT64,
            attributes=(("target", "int64"),),
        ),
        _op(
            "op10",
            "body",
            "binary.add",
            ("%count", "%increment"),
            result_id="%next_count",
            result_type=INT64,
        ),
        _op(
            "op11",
            "body",
            "binary.add",
            ("%index", "%one"),
            result_id="%next_index",
            result_type=INT64,
        ),
        _op(
            "op12",
            "body",
            "jump",
            attributes=(("target_block", "header"),),
        ),
        _op(
            "op13",
            "exit",
            "return",
            ("%result",),
        ),
    )
    return build_typed_module(
        function_id="a" * 64,
        entry_block="entry",
        input_types=(EXACT_UNICODE,),
        output_type=INT64,
        blocks=(
            TypedBlock("entry", (), ("op0", "op1", "op2", "op3", "op4")),
            TypedBlock(
                "header",
                (
                    TypedBlockArgument("%index", INT64),
                    TypedBlockArgument("%count", INT64),
                ),
                ("op5", "op6"),
            ),
            TypedBlock("body", (), ("op7", "op8", "op9", "op10", "op11", "op12")),
            TypedBlock(
                "exit",
                (TypedBlockArgument("%result", INT64),),
                ("op13",),
            ),
        ),
        control_edges=(
            TypedControlEdge("entry", "header", "jump", ("%zero", "%zero")),
            TypedControlEdge("header", "body", "branch_true", ()),
            TypedControlEdge("header", "exit", "branch_false", ("%count",)),
            TypedControlEdge(
                "body",
                "header",
                "jump",
                ("%next_index", "%next_count"),
            ),
        ),
        operations=operations,
        return_operation_id="op13",
    )


def _integer_sum_module() -> TypedSemanticModule:
    sequence_type = TypeSpec(
        TypeKind.SEQUENCE,
        "list",
        (INT64,),
        Exactness.EXACT,
        "python_object",
    )
    operations = (
        _op("op0", "entry", "argument", result_id="%items", result_type=sequence_type, attributes=(("index", "0"),)),
        _op("op1", "entry", "constant", result_id="%zero", result_type=INT64, literal=SemanticLiteral.from_value(0)),
        _op("op2", "entry", "constant", result_id="%one", result_type=INT64, literal=SemanticLiteral.from_value(1)),
        _op("op3", "entry", "sequence.length", ("%items",), result_id="%length", result_type=INT64),
        _op("op4", "entry", "jump", attributes=(("target_block", "header"),)),
        _op("op5", "header", "compare.lt", ("%index", "%length"), result_id="%continue", result_type=BOOL),
        _op("op6", "header", "branch", ("%continue",), attributes=(("false_block", "exit"), ("true_block", "body"))),
        _op("op7", "body", "sequence.get", ("%items", "%index"), result_id="%item", result_type=INT64, may_raise=True, exception_order=0),
        _op("op8", "body", "binary.add", ("%total", "%item"), result_id="%next_total", result_type=INT64),
        _op("op9", "body", "binary.add", ("%index", "%one"), result_id="%next_index", result_type=INT64),
        _op("op10", "body", "jump", attributes=(("target_block", "header"),)),
        _op("op11", "exit", "return", ("%result",)),
    )
    return build_typed_module(
        function_id="b" * 64,
        entry_block="entry",
        input_types=(sequence_type,),
        output_type=INT64,
        blocks=(
            TypedBlock("entry", (), ("op0", "op1", "op2", "op3", "op4")),
            TypedBlock("header", (TypedBlockArgument("%index", INT64), TypedBlockArgument("%total", INT64)), ("op5", "op6")),
            TypedBlock("body", (), ("op7", "op8", "op9", "op10")),
            TypedBlock("exit", (TypedBlockArgument("%result", INT64),), ("op11",)),
        ),
        control_edges=(
            TypedControlEdge("entry", "header", "jump", ("%zero", "%zero")),
            TypedControlEdge("header", "body", "branch_true", ()),
            TypedControlEdge("header", "exit", "branch_false", ("%total",)),
            TypedControlEdge("body", "header", "jump", ("%next_index", "%next_total")),
        ),
        operations=operations,
        return_operation_id="op11",
    )


class TypedSemanticIrV2Tests(unittest.TestCase):
    def test_unicode_loop_round_trips_and_executes(self) -> None:
        module = _unicode_count_module()

        decoded = TypedSemanticModule.from_document(module.to_document())

        self.assertEqual(decoded, module)
        self.assertEqual(execute_typed_module(decoded, ("a中-Ⅷ",)), 3)
        self.assertEqual(execute_typed_module(decoded, ("",)), 0)

    def test_runtime_dependency_hashes_are_portable_and_hashed(self) -> None:
        module = build_typed_module(
            function_id="c" * 64,
            entry_block="entry",
            input_types=(INT64,),
            output_type=INT64,
            blocks=(TypedBlock("entry", (), ("op0", "op1")),),
            control_edges=(),
            operations=(
                _op(
                    "op0",
                    "entry",
                    "argument",
                    result_id="%value",
                    result_type=INT64,
                    attributes=(("index", "0"),),
                ),
                _op("op1", "entry", "return", ("%value",)),
            ),
            return_operation_id="op1",
            runtime_dependency_hashes=("d" * 64,),
        )

        decoded = TypedSemanticModule.from_document(module.to_document())

        self.assertEqual(decoded.runtime_dependency_hashes, ("d" * 64,))
        self.assertEqual(decoded, module)

    def test_runtime_dependency_hashes_must_be_sorted_and_unique(self) -> None:
        module = _integer_sum_module()
        invalid = rehash_typed_module(
            dataclasses.replace(
                module,
                runtime_dependency_hashes=("f" * 64, "e" * 64),
            )
        )

        with self.assertRaisesRegex(TypedVerificationError, "invalid_dependencies"):
            verify_typed_module(invalid)

    def test_same_cfg_contract_executes_non_text_reduction(self) -> None:
        module = _integer_sum_module()

        self.assertEqual(execute_typed_module(module, ([3, -1, 4],)), 6)
        self.assertEqual(execute_typed_module(module, ([],)), 0)

    def test_edge_arguments_must_match_target_block_types(self) -> None:
        module = _unicode_count_module()
        edges = list(module.control_edges)
        edges[0] = dataclasses.replace(
            edges[0],
            arguments=("%continue", "%zero"),
        )
        invalid = rehash_typed_module(
            dataclasses.replace(module, control_edges=tuple(edges))
        )

        with self.assertRaisesRegex(
            TypedVerificationError,
            "edge_argument_type_mismatch",
        ):
            verify_typed_module(invalid)

    def test_backedge_value_must_dominate_source_terminator(self) -> None:
        module = _unicode_count_module()
        edges = list(module.control_edges)
        edges[-1] = dataclasses.replace(
            edges[-1],
            arguments=("%next_index", "%result"),
        )
        invalid = rehash_typed_module(
            dataclasses.replace(module, control_edges=tuple(edges))
        )

        with self.assertRaisesRegex(
            TypedVerificationError,
            "operand_not_available",
        ):
            verify_typed_module(invalid)

    def test_exact_type_guard_rejects_subclasses_before_execution(self) -> None:
        class Text(str):
            pass

        with self.assertRaisesRegex(TypeError, "exact_type_guard_failed"):
            execute_typed_module(_unicode_count_module(), (Text("abc"),))


if __name__ == "__main__":
    unittest.main()
