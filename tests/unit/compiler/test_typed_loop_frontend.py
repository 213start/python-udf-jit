from __future__ import annotations

import dataclasses
import json
import inspect
import re
import unittest
from unittest import mock

from python_udf_jit.compiler.typed_analysis import analyze_typed_module
from python_udf_jit.compiler.typed_frontend import (
    TypedCaptureError,
    capture_typed_loop,
)
from python_udf_jit.compiler.typed_ir import (
    BOOL,
    EXACT_UNICODE,
    FLOAT64,
    INT64,
    Exactness,
    TypeKind,
    TypeSpec,
    rehash_typed_module,
)
from python_udf_jit.compiler.typed_reference import execute_typed_module
from python_udf_jit.compiler.typed_verifier import (
    TypedVerificationError,
    verify_typed_module,
)


def generator_ratio(text: str, min_ratio: float = 0.5) -> bool:
    return sum(1 for character in text if character.isalnum()) / len(text) >= min_ratio


def explicit_ratio(value: str, min_ratio: float = 0.5) -> bool:
    accepted = 0
    for codepoint in value:
        if codepoint.isalnum():
            accepted += 1
    return accepted / len(value) >= min_ratio


def numeric_total(items: list[int]) -> int:
    total = 0
    for item in items:
        total += item
    return total


def numeric_total_replacement(items: list[int]) -> int:
    total = 0
    for _item in items:
        total += 1
    return total


def unused_required_positional(items: list[int], unused: int) -> int:
    total = 0
    for item in items:
        total += item
    return total


def unused_required_keyword(items: list[int], *, unused: int) -> int:
    total = 0
    for item in items:
        total += item
    return total


def multiple_defaults(
    items: list[int],
    lower: int = 2,
    upper: int = 5,
) -> int:
    total = 0
    for item in items:
        total += item
    return total + lower + upper


def signed_zero_product(
    items: list[float],
    direction: float = -0.0,
) -> float:
    total = 0.0
    for item in items:
        total += item
    return total * direction


def positive_count(items: list[int]) -> int:
    total = 0
    for item in items:
        if item > 0:
            total += 1
    return total


def seeded_total(items: list[int]) -> int:
    total = 7
    for item in items:
        total += item
    return total


def seeded_float_total(items: list[float]) -> float:
    total = 1.5
    for item in items:
        total += item
    return total


_DYNAMIC_THRESHOLD = 0


def count_above_dynamic_threshold(items: list[int]) -> int:
    total = 0
    for item in items:
        if item > _DYNAMIC_THRESHOLD:
            total += 1
    return total


def two_sum_calls(text: str) -> int:
    return sum(1 for character in text if character.isalpha()) + sum([1])


def keyword_len(items: list[int]) -> float:
    total = 0
    for item in items:
        total += item
    return total / len(obj=items)


def _closure_threshold_counter():
    threshold = 0

    def counter(items: list[int]) -> int:
        total = 0
        for item in items:
            if item > threshold:
                total += 1
        return total

    def set_threshold(value: int) -> None:
        nonlocal threshold
        threshold = value

    return counter, set_threshold


def unsupported_side_effect(items: list[int]) -> int:
    total = 0
    for item in items:
        print(item)
        total += item
    return total


def guarded_bound_ratio(text: str, *, minimum: float) -> bool:
    if not text:
        return False
    accepted = sum(1 for character in text if character.isalnum())
    return accepted / len(text) >= minimum


def punctuation_transform(text: str) -> str:
    table = str.maketrans(
        {
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
            "\u2013": "-",
            "\u2014": "-",
        }
    )
    return text.translate(table)


def unrelated_symbol_remap(payload: str) -> str:
    substitutions = str.maketrans({"α": "a", "β": "b", "→": ">"})
    return payload.translate(substitutions)


def input_shadowing_translation(text: str) -> str:
    text = str.maketrans({"α": "a"})
    return text.translate(text)


_SPACE_RUN = re.compile(r"\s+")


def whitespace_transform(text: str) -> str:
    return _SPACE_RUN.sub(" ", text).strip()


def unrelated_run_collapse(payload: str) -> str:
    return _SPACE_RUN.sub("\u2003", payload).strip()


def unsupported_expanding_translation(text: str) -> str:
    table = str.maketrans({"…": "..."})
    return text.translate(table)


def unsupported_nonspace_replacement(text: str) -> str:
    return _SPACE_RUN.sub("_", text).strip()


class TypedLoopFrontendTests(unittest.TestCase):
    @staticmethod
    def _integer_sequence() -> TypeSpec:
        return TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (INT64,),
            Exactness.EXACT,
            "python_object",
        )

    def test_generator_and_explicit_loop_normalize_to_same_patterns(self) -> None:
        generator = capture_typed_loop(
            generator_ratio,
            input_types=(EXACT_UNICODE,),
        )
        explicit = capture_typed_loop(
            explicit_ratio,
            input_types=(EXACT_UNICODE,),
        )

        generator_analysis = analyze_typed_module(generator.module)
        explicit_analysis = analyze_typed_module(explicit.module)
        self.assertEqual(
            [value.kind for value in generator_analysis.patterns.loops],
            [value.kind for value in explicit_analysis.patterns.loops],
        )
        self.assertEqual(
            [value.operation for value in generator_analysis.patterns.reductions],
            [value.operation for value in explicit_analysis.patterns.reductions],
        )
        self.assertEqual(generator.normalized_pattern, "iterator_reduction")
        self.assertEqual(explicit.normalized_pattern, "iterator_reduction")

        for text in ("abc-", "中文 12", "---", "Ⅷ²四"):
            self.assertEqual(
                execute_typed_module(generator.module, (text,)),
                generator_ratio(text),
            )
            self.assertEqual(
                execute_typed_module(explicit.module, (text,)),
                explicit_ratio(text),
            )

    def test_scalar_translation_normalizes_to_immutable_lookup_and_builder(
        self,
    ) -> None:
        for function in (punctuation_transform, unrelated_symbol_remap):
            with self.subTest(function=function.__name__):
                captured = capture_typed_loop(
                    function,
                    input_types=(EXACT_UNICODE,),
                )

                operations = {operation.op for operation in captured.module.operations}
                self.assertEqual(
                    captured.normalized_pattern,
                    "iterator_immutable_lookup_builder",
                )
                self.assertIn("immutable.lookup", operations)
                self.assertIn("sequence.builder.append", operations)
                self.assertIn("sequence.builder.finish", operations)
                create = next(
                    operation
                    for operation in captured.module.operations
                    if operation.op == "sequence.builder.create"
                )
                length = next(
                    operation
                    for operation in captured.module.operations
                    if operation.op == "sequence.length"
                )
                self.assertEqual(create.operands, (length.result_id,))
                self.assertEqual(
                    captured.analysis.behavior.family.value,
                    "sequence_transform",
                )
                for value in ("", "plain ASCII", "“α—β” → ‘x’", "中文\u2003"):
                    self.assertEqual(
                        execute_typed_module(captured.module, (value,)),
                        function(value),
                    )

        encoded = json.dumps(
            capture_typed_loop(
                punctuation_transform,
                input_types=(EXACT_UNICODE,),
            ).module.to_document(),
            sort_keys=True,
        ).lower()
        self.assertNotIn("punctuation", encoded)

    def test_translation_capture_proves_the_input_binding_is_not_rebound(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            TypedCaptureError,
            "translation_input_binding_rebound",
        ):
            capture_typed_loop(
                input_shadowing_translation,
                input_types=(EXACT_UNICODE,),
            )

        captured = capture_typed_loop(
            unrelated_symbol_remap,
            input_types=(EXACT_UNICODE,),
        )
        self.assertEqual(
            captured.normalized_pattern,
            "iterator_immutable_lookup_builder",
        )

    def test_unicode_space_collapse_normalizes_to_fsm_and_builder(self) -> None:
        for function in (whitespace_transform, unrelated_run_collapse):
            with self.subTest(function=function.__name__):
                captured = capture_typed_loop(
                    function,
                    input_types=(EXACT_UNICODE,),
                )

                operations = {operation.op for operation in captured.module.operations}
                self.assertEqual(
                    captured.normalized_pattern,
                    "iterator_unicode_fsm_builder",
                )
                self.assertIn("unicode.property", operations)
                self.assertIn("fsm.transition", operations)
                self.assertIn("sequence.builder.apply", operations)
                self.assertEqual(
                    captured.analysis.behavior.family.value,
                    "branch_fsm",
                )
                self.assertTrue(captured.analysis.patterns.fsm_operations)
                for value in (
                    "",
                    "plain",
                    "  a\t\nb  ",
                    "\u2003中文\u2029  text\u3000",
                    "\x1cA\x1fB\x85",
                ):
                    self.assertEqual(
                        execute_typed_module(captured.module, (value,)),
                        function(value),
                    )

    def test_transform_subset_rejects_semantics_it_cannot_preserve(self) -> None:
        with self.assertRaisesRegex(
            TypedCaptureError,
            "translation_value_unsupported",
        ):
            capture_typed_loop(
                unsupported_expanding_translation,
                input_types=(EXACT_UNICODE,),
            )
        with self.assertRaisesRegex(
            TypedCaptureError,
            "regex_replacement_unsupported",
        ):
            capture_typed_loop(
                unsupported_nonspace_replacement,
                input_types=(EXACT_UNICODE,),
            )

    def test_regex_binding_is_part_of_the_runtime_guard(self) -> None:
        global _SPACE_RUN
        captured = capture_typed_loop(
            whitespace_transform,
            input_types=(EXACT_UNICODE,),
        )
        original = _SPACE_RUN
        try:
            _SPACE_RUN = re.compile(r"(?:\s)+")
            self.assertFalse(captured.runtime_guard.matches())
        finally:
            _SPACE_RUN = original

    def test_transform_descriptors_are_canonical_and_verified(self) -> None:
        cases = (
            (punctuation_transform, "immutable.lookup", "keys", "[8216, 8217]"),
            (whitespace_transform, "fsm.transition", "transitions", "[1,0]"),
        )
        for function, op_name, attribute_name, invalid_value in cases:
            with self.subTest(function=function.__name__):
                module = capture_typed_loop(
                    function,
                    input_types=(EXACT_UNICODE,),
                ).module
                operations = tuple(
                    dataclasses.replace(
                        operation,
                        attributes=tuple(
                            sorted(
                                (
                                    key,
                                    invalid_value if key == attribute_name else value,
                                )
                                for key, value in operation.attributes
                            )
                        ),
                    )
                    if operation.op == op_name
                    else operation
                    for operation in module.operations
                )
                invalid = rehash_typed_module(
                    dataclasses.replace(module, operations=operations)
                )

                with self.assertRaisesRegex(
                    TypedVerificationError,
                    "invalid_attributes|type_mismatch",
                ):
                    verify_typed_module(invalid)

    def test_bound_argument_and_guarded_region_normalize_generically(self) -> None:
        captured = capture_typed_loop(
            guarded_bound_ratio,
            input_types=(EXACT_UNICODE,),
            bound_arguments={"minimum": 0.5},
            allow_guarded_region=True,
        )

        self.assertIsNotNone(captured.entry_guard)
        self.assertFalse(captured.entry_guard.matches(("",)))
        self.assertTrue(captured.entry_guard.matches(("abc-",)))
        self.assertEqual(
            execute_typed_module(captured.module, ("abc-",)),
            guarded_bound_ratio("abc-", minimum=0.5),
        )
        self.assertIn(
            "bound_argument",
            {dependency.kind for dependency in captured.runtime_guard.dependencies},
        )

    def test_guarded_region_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(TypedCaptureError):
            capture_typed_loop(
                guarded_bound_ratio,
                input_types=(EXACT_UNICODE,),
                bound_arguments={"minimum": 0.5},
            )

    def test_bound_argument_changes_semantic_identity(self) -> None:
        lower = capture_typed_loop(
            guarded_bound_ratio,
            input_types=(EXACT_UNICODE,),
            bound_arguments={"minimum": 0.2},
            allow_guarded_region=True,
        )
        higher = capture_typed_loop(
            guarded_bound_ratio,
            input_types=(EXACT_UNICODE,),
            bound_arguments={"minimum": 0.8},
            allow_guarded_region=True,
        )

        self.assertNotEqual(lower.module.semantic_hash, higher.module.semantic_hash)
        self.assertTrue(lower.runtime_guard.matches())
        self.assertTrue(higher.runtime_guard.matches())

    def test_numeric_loop_uses_same_frontend_without_text_knowledge(self) -> None:
        sequence_type = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (INT64,),
            Exactness.EXACT,
            "python_object",
        )
        captured = capture_typed_loop(
            numeric_total,
            input_types=(sequence_type,),
        )

        self.assertEqual(captured.module.output_type, INT64)
        self.assertEqual(execute_typed_module(captured.module, ([2, -3, 8],)), 7)
        self.assertEqual(captured.normalized_pattern, "iterator_reduction")

    def test_numeric_predicate_is_a_typed_primitive_not_a_business_case(self) -> None:
        sequence_type = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (INT64,),
            Exactness.EXACT,
            "python_object",
        )

        captured = capture_typed_loop(
            positive_count,
            input_types=(sequence_type,),
        )

        self.assertEqual(execute_typed_module(captured.module, ([2, -3, 8],)), 2)
        self.assertIn(
            "compare.gt",
            {operation.op for operation in captured.module.operations},
        )

    def test_capture_records_source_identity_without_business_name_matching(self) -> None:
        captured = capture_typed_loop(
            generator_ratio,
            input_types=(EXACT_UNICODE,),
        )

        self.assertEqual(len(captured.code_sha256), 64)
        self.assertEqual(len(captured.source_sha256), 64)
        self.assertNotIn(
            "generator_ratio",
            json.dumps(captured.module.to_document(), sort_keys=True),
        )
        self.assertEqual(captured.module.output_type, BOOL)

    def test_unknown_effect_in_loop_is_rejected(self) -> None:
        sequence_type = self._integer_sequence()

        with self.assertRaisesRegex(
            TypedCaptureError,
            "loop_body_effect_unsupported",
        ):
            capture_typed_loop(
                unsupported_side_effect,
                input_types=(sequence_type,),
            )

    def test_nonzero_and_float_seeds_do_not_seed_the_loop_index(self) -> None:
        integer = capture_typed_loop(
            seeded_total,
            input_types=(self._integer_sequence(),),
        )
        float_sequence = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (FLOAT64,),
            Exactness.EXACT,
            "python_object",
        )
        floating = capture_typed_loop(
            seeded_float_total,
            input_types=(float_sequence,),
        )

        self.assertEqual(execute_typed_module(integer.module, ([2, -3, 8],)), 14)
        self.assertEqual(
            execute_typed_module(floating.module, ([2.0, -3.0, 8.0],)),
            8.5,
        )

    def test_capture_embeds_runtime_dependencies_and_guard_rechecks_them(self) -> None:
        captured = capture_typed_loop(
            generator_ratio,
            input_types=(EXACT_UNICODE,),
        )

        self.assertEqual(
            captured.module.runtime_dependency_hashes,
            captured.runtime_guard.dependency_hashes,
        )
        self.assertTrue(captured.runtime_guard.matches())
        original_defaults = generator_ratio.__defaults__
        original_sum = generator_ratio.__globals__.get("sum", None)
        had_sum = "sum" in generator_ratio.__globals__
        try:
            generator_ratio.__defaults__ = (0.9,)
            self.assertFalse(captured.runtime_guard.matches())
            generator_ratio.__defaults__ = original_defaults
            generator_ratio.__globals__["sum"] = lambda values: 0
            self.assertFalse(captured.runtime_guard.matches())
        finally:
            generator_ratio.__defaults__ = original_defaults
            if had_sum:
                generator_ratio.__globals__["sum"] = original_sum
            else:
                generator_ratio.__globals__.pop("sum", None)

    def test_additional_required_parameters_are_rejected(self) -> None:
        for function, name in (
            (unused_required_positional, "unused"),
            (unused_required_keyword, "unused"),
        ):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(
                    TypedCaptureError,
                    f"additional_parameter_required:{name}",
                ):
                    capture_typed_loop(
                        function,
                        input_types=(self._integer_sequence(),),
                    )

    def test_capture_rejects_live_source_default_layout_mismatch(self) -> None:
        original_defaults = multiple_defaults.__defaults__
        try:
            multiple_defaults.__defaults__ = (2, 5, 5)
            with self.assertRaisesRegex(
                TypedCaptureError,
                "function_default_layout_mismatch",
            ):
                capture_typed_loop(
                    multiple_defaults,
                    input_types=(self._integer_sequence(),),
                )
        finally:
            multiple_defaults.__defaults__ = original_defaults

    def test_positional_default_guard_tracks_logical_parameter_layout(self) -> None:
        captured = capture_typed_loop(
            multiple_defaults,
            input_types=(self._integer_sequence(),),
        )
        original_defaults = multiple_defaults.__defaults__
        mutations = (
            (2, 5, 5),
            (5,),
            (5, 2),
            (2, 9),
        )
        try:
            for defaults in mutations:
                with self.subTest(defaults=defaults):
                    multiple_defaults.__defaults__ = defaults
                    self.assertFalse(captured.runtime_guard.matches())
            multiple_defaults.__defaults__ = original_defaults
            self.assertTrue(captured.runtime_guard.matches())
        finally:
            multiple_defaults.__defaults__ = original_defaults

    def test_runtime_guard_rechecks_the_function_code_identity(self) -> None:
        captured = capture_typed_loop(
            numeric_total,
            input_types=(self._integer_sequence(),),
        )
        with (
            mock.patch(
                "python_udf_jit.compiler.typed_frontend.code_identity"
            ) as hash_function,
            mock.patch(
                "python_udf_jit.compiler.typed_frontend.code_identity_from_code"
            ) as hash_code,
        ):
            self.assertTrue(captured.runtime_guard.matches())
        hash_function.assert_not_called()
        hash_code.assert_not_called()
        original_code = numeric_total.__code__
        try:
            numeric_total.__code__ = numeric_total_replacement.__code__
            self.assertFalse(captured.runtime_guard.matches())
            self.assertEqual(captured.code_sha256, captured.module.function_id)
            self.assertEqual(
                captured.module.runtime_dependency_hashes,
                captured.runtime_guard.dependency_hashes,
            )
        finally:
            numeric_total.__code__ = original_code

    def test_signed_zero_constants_remain_distinct_in_capture_and_reference(
        self,
    ) -> None:
        float_sequence = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (FLOAT64,),
            Exactness.EXACT,
            "python_object",
        )
        captured = capture_typed_loop(
            signed_zero_product,
            input_types=(float_sequence,),
        )
        float_literals = {
            operation.literal.encoded_value
            for operation in captured.module.operations
            if operation.literal is not None
            and operation.literal.kind.value == "float64"
        }

        self.assertIn("0x0.0p+0", float_literals)
        self.assertIn("-0x0.0p+0", float_literals)
        self.assertEqual(
            execute_typed_module(captured.module, ([],)).hex(),
            signed_zero_product([]).hex(),
        )

    def test_global_constant_dependency_is_rechecked(self) -> None:
        global _DYNAMIC_THRESHOLD
        captured = capture_typed_loop(
            count_above_dynamic_threshold,
            input_types=(self._integer_sequence(),),
        )

        original = _DYNAMIC_THRESHOLD
        try:
            _DYNAMIC_THRESHOLD = 4
            self.assertFalse(captured.runtime_guard.matches())
        finally:
            _DYNAMIC_THRESHOLD = original

    def test_closure_constant_dependency_is_rechecked(self) -> None:
        counter, set_threshold = _closure_threshold_counter()
        captured = capture_typed_loop(
            counter,
            input_types=(self._integer_sequence(),),
        )

        self.assertTrue(captured.runtime_guard.matches())
        set_threshold(4)
        self.assertFalse(captured.runtime_guard.matches())

    def test_builtin_calls_require_the_exact_builtin_binding(self) -> None:
        namespace = generator_ratio.__globals__
        original = namespace.get("sum")
        had_sum = "sum" in namespace
        namespace["sum"] = lambda values: 0
        try:
            with self.assertRaisesRegex(
                TypedCaptureError,
                "builtin_binding_not_exact:sum",
            ):
                capture_typed_loop(
                    generator_ratio,
                    input_types=(EXACT_UNICODE,),
                )
        finally:
            if had_sum:
                namespace["sum"] = original
            else:
                namespace.pop("sum", None)

    def test_only_the_recognized_generator_sum_is_replaced(self) -> None:
        with self.assertRaisesRegex(
            TypedCaptureError,
            "return_expression_unsupported",
        ):
            capture_typed_loop(two_sum_calls, input_types=(EXACT_UNICODE,))

    def test_len_keywords_are_not_silently_dropped(self) -> None:
        with self.assertRaisesRegex(
            TypedCaptureError,
            "return_expression_unsupported",
        ):
            capture_typed_loop(
                keyword_len,
                input_types=(self._integer_sequence(),),
            )

    def test_source_must_describe_the_live_code_object(self) -> None:
        lines, first_line = inspect.getsourcelines(numeric_total)
        stale = [line.replace("total += item", "total += 1") for line in lines]

        with mock.patch(
            "python_udf_jit.compiler.typed_frontend.inspect.getsourcelines",
            return_value=(stale, first_line),
        ):
            with self.assertRaisesRegex(
                TypedCaptureError,
                "source_code_identity_mismatch",
            ):
                capture_typed_loop(
                    numeric_total,
                    input_types=(self._integer_sequence(),),
                )

    def test_every_operation_has_an_absolute_source_line(self) -> None:
        captured = capture_typed_loop(
            explicit_ratio,
            input_types=(EXACT_UNICODE,),
        )

        offsets = [operation.source_offset for operation in captured.module.operations]
        self.assertTrue(all(type(value) is int for value in offsets))
        self.assertTrue(all(value >= captured.source_first_line for value in offsets))


if __name__ == "__main__":
    unittest.main()
