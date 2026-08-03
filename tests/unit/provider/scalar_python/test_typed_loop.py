from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest import mock

from python_udf_jit.compiler.typed_frontend import capture_typed_loop
from python_udf_jit.compiler.typed_ir import (
    EXACT_UNICODE,
    FLOAT64,
    INT64,
    Exactness,
    TypeKind,
    TypeSpec,
    build_typed_module,
)
from python_udf_jit.provider.scalar_python.typed_loop import (
    BackendCompilation,
    CinderXTypedLoopBackend,
    CompileStatus,
    RuntimeFeedback,
    TypedGuardMiss,
    TypedLoweringError,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
    lower_unicode_count_physical,
    lower_typed_loop,
)


def generator_ratio(text: str, threshold: float = 0.5) -> bool:
    return sum(1 for character in text if character.isalnum()) / len(text) >= threshold


def explicit_ratio(value: str, threshold: float = 0.5) -> bool:
    accepted = 0
    for character in value:
        if character.isalnum():
            accepted += 1
    return accepted / len(value) >= threshold


def numeric_total(items: list[int]) -> int:
    total = 0
    for item in items:
        total += item
    return total


def replacement_generator_ratio(text: str, threshold: float = 0.5) -> bool:
    return len(text) >= threshold


def signed_zero_product(
    items: list[float],
    direction: float = -0.0,
) -> float:
    total = 0.0
    for item in items:
        total += item
    return total * direction


def alpha_count(text: str) -> int:
    return sum(1 for character in text if character.isalpha())


def space_count(text: str) -> int:
    accepted = 0
    for character in text:
        if character.isspace():
            accepted += 1
    return accepted


class _StringSubclass(str):
    pass


class _ListSubclass(list[int]):
    pass


class _Backend:
    adapter_version = "test-adapter-v1"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.documents: list[dict[str, object]] = []

    def compile(self, lowering) -> BackendCompilation:
        self.calls += 1
        self.documents.append(lowering.plan.to_document())
        if self.fail:
            raise RuntimeError("synthetic backend failure")
        return BackendCompilation(
            True,
            "test_hir_adapter",
            (("Phi", 2), ("PrimitiveCompare", 1)),
        )


class TypedLoopLoweringTests(unittest.TestCase):
    def test_two_source_shapes_lower_to_the_same_generic_plan(self) -> None:
        generator = lower_typed_loop(
            capture_typed_loop(
                generator_ratio,
                input_types=(EXACT_UNICODE,),
            ).module
        )
        explicit = lower_typed_loop(
            capture_typed_loop(
                explicit_ratio,
                input_types=(EXACT_UNICODE,),
            ).module
        )

        self.assertEqual(
            generator.plan.pattern_kind,
            explicit.plan.pattern_kind,
        )
        self.assertEqual(
            generator.plan.iterator_strategy,
            explicit.plan.iterator_strategy,
        )
        self.assertEqual(
            generator.plan.reduction_operation,
            explicit.plan.reduction_operation,
        )
        for text in ("abc-", "中文 12", "---", "Ⅷ²四"):
            self.assertEqual(generator.function(text), generator_ratio(text))
            self.assertEqual(explicit.function(text), explicit_ratio(text))

    def test_type_specialization_is_an_independent_plan_dimension(self) -> None:
        sequence_type = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (INT64,),
            Exactness.EXACT,
            "python_object",
        )
        text = lower_typed_loop(
            capture_typed_loop(
                explicit_ratio,
                input_types=(EXACT_UNICODE,),
            ).module
        )
        numeric = lower_typed_loop(
            capture_typed_loop(
                numeric_total,
                input_types=(sequence_type,),
            ).module
        )

        self.assertEqual(text.plan.pattern_kind, numeric.plan.pattern_kind)
        self.assertNotEqual(text.plan.iterator_strategy, numeric.plan.iterator_strategy)
        self.assertEqual(numeric.function([2, -3, 8]), 7)
        with self.assertRaises(TypedGuardMiss):
            text.function(_StringSubclass("abc"))
        with self.assertRaises(TypedGuardMiss):
            numeric.function(_ListSubclass([1, 2]))

    def test_lowering_preserves_signed_zero_constants(self) -> None:
        float_sequence = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (FLOAT64,),
            Exactness.EXACT,
            "python_object",
        )
        lowering = lower_typed_loop(
            capture_typed_loop(
                signed_zero_product,
                input_types=(float_sequence,),
            ).module
        )

        self.assertEqual(
            lowering.function([]).hex(),
            signed_zero_product([]).hex(),
        )

    def test_lowering_artifacts_map_every_semantic_operation(self) -> None:
        captured = capture_typed_loop(
            explicit_ratio,
            input_types=(EXACT_UNICODE,),
        )

        lowering = lower_typed_loop(captured.module)

        self.assertEqual(
            {operation.operation_id for operation in captured.module.operations},
            {operation_id for operation_id, _ in lowering.operation_lines},
        )
        self.assertEqual(len(lowering.generated_code_hash), 64)
        self.assertNotIn("explicit_ratio", lowering.generated_source)

    def test_unicode_physicalization_is_shared_across_shapes_and_properties(
        self,
    ) -> None:
        methods = (
            "isalnum",
            "isalpha",
            "isdecimal",
            "isdigit",
            "isnumeric",
            "isspace",
        )
        calls: list[tuple[str, int]] = []

        def helper(text: str, property_id: int) -> int:
            calls.append((text, property_id))
            return sum(getattr(character, methods[property_id])() for character in text)

        cases = (
            (generator_ratio, 0),
            (explicit_ratio, 0),
            (alpha_count, 1),
            (space_count, 5),
        )
        for function, property_id in cases:
            with self.subTest(function=function.__name__):
                captured = capture_typed_loop(
                    function,
                    input_types=(EXACT_UNICODE,),
                )
                generic = lower_typed_loop(captured.module)
                physical = lower_unicode_count_physical(generic, helper)
                text = "A²⅕٣ 中\u2003"
                self.assertEqual(physical.function(text), function(text))
                self.assertEqual(physical.property_id, property_id)
                self.assertEqual(calls[-1], (text, property_id))
                self.assertEqual(
                    {operation.operation_id for operation in captured.module.operations},
                    {operation_id for operation_id, _ in physical.operation_lines},
                )
                self.assertNotIn("for ", physical.generated_source)
                self.assertNotIn("while ", physical.generated_source)
                self.assertNotIn(function.__name__, physical.generated_source)

        self.assertEqual(len(calls), len(cases))

    def test_unicode_physicalization_preserves_exact_type_guard(self) -> None:
        lowering = lower_typed_loop(
            capture_typed_loop(
                alpha_count,
                input_types=(EXACT_UNICODE,),
            ).module
        )
        physical = lower_unicode_count_physical(
            lowering,
            lambda text, _property: sum(character.isalpha() for character in text),
        )

        with self.assertRaises(TypedGuardMiss):
            physical.function(_StringSubclass("abc"))

    def test_non_unicode_loop_does_not_enter_unicode_physicalization(self) -> None:
        sequence_type = TypeSpec(
            TypeKind.SEQUENCE,
            "list",
            (INT64,),
            Exactness.EXACT,
            "python_object",
        )
        lowering = lower_typed_loop(
            capture_typed_loop(
                numeric_total,
                input_types=(sequence_type,),
            ).module
        )

        with self.assertRaisesRegex(
            TypedLoweringError,
            "unicode_count_kernel_shape_mismatch",
        ):
            lower_unicode_count_physical(lowering, lambda _text, _property: 0)

    def test_physicalization_proves_the_induction_step(self) -> None:
        captured = capture_typed_loop(
            alpha_count,
            input_types=(EXACT_UNICODE,),
        )
        module = captured.module
        zero = next(
            operation.result_id
            for operation in module.operations
            if operation.op == "constant"
            and operation.literal is not None
            and type(operation.literal.value) is int
            and operation.literal.value == 0
        )
        operations = tuple(
            replace(
                operation,
                operands=("%index", zero),
            )
            if operation.block_id == "body"
            and operation.op == "binary.add"
            and "%index" in operation.operands
            else operation
            for operation in module.operations
        )
        malformed = build_typed_module(
            function_id=module.function_id,
            entry_block=module.entry_block,
            input_types=module.input_types,
            output_type=module.output_type,
            blocks=module.blocks,
            control_edges=module.control_edges,
            operations=operations,
            return_operation_id=module.return_operation_id,
        )

        with self.assertRaisesRegex(
            TypedLoweringError,
            "unicode_count_induction_increment_invalid",
        ):
            lower_unicode_count_physical(
                lower_typed_loop(malformed),
                lambda _text, _property: 0,
            )


class TypedRegionCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = capture_typed_loop(
            generator_ratio,
            input_types=(EXACT_UNICODE,),
        )

    def test_worker_recomputes_analysis_and_compiles_after_roi_gate(self) -> None:
        backend = _Backend()
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=10,
            negative_ttl_ns=1_000_000_000,
        )
        request = TypedRegionCompileRequest(
            self.capture.module,
            RuntimeFeedback(call_count=10, deopt_count=0),
            driver_analysis_hint={"untrusted": True},
            runtime_guard=self.capture.runtime_guard,
        )

        decision = compiler.compile(request)

        self.assertEqual(decision.status, CompileStatus.COMPILED)
        self.assertIsNotNone(decision.variant)
        self.assertFalse(decision.driver_analysis_hint_matched)
        self.assertEqual(decision.variant("abc-"), generator_ratio("abc-"))
        self.assertEqual(backend.calls, 1)
        self.assertNotIn("fineweb", str(backend.documents).lower())

    def test_low_call_count_is_deferred_without_lowering_or_backend_work(self) -> None:
        backend = _Backend()
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=10,
            negative_ttl_ns=1_000_000_000,
        )

        decision = compiler.compile(
            TypedRegionCompileRequest(
                self.capture.module,
                RuntimeFeedback(call_count=9, deopt_count=0),
                runtime_guard=self.capture.runtime_guard,
            )
        )

        self.assertEqual(decision.status, CompileStatus.DEFERRED)
        self.assertEqual(decision.reason_code, "runtime_call_threshold")
        self.assertEqual(backend.calls, 0)

    def test_deterministic_backend_failure_is_negative_cached(self) -> None:
        backend = _Backend(fail=True)
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=1,
            negative_ttl_ns=1_000_000_000,
        )
        request = TypedRegionCompileRequest(
            self.capture.module,
            RuntimeFeedback(call_count=1, deopt_count=0),
            runtime_guard=self.capture.runtime_guard,
        )

        first = compiler.compile(request)
        second = compiler.compile(request)

        self.assertEqual(first.status, CompileStatus.FAILURE)
        self.assertEqual(first.reason_code, "backend_compile_failed")
        self.assertEqual(second.status, CompileStatus.NEGATIVE_CACHE)
        self.assertEqual(second.reason_code, "backend_compile_failed")
        self.assertEqual(backend.calls, 1)

    def test_successful_compilation_is_cached_without_recompiling_backend(self) -> None:
        backend = _Backend()
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=1,
            negative_ttl_ns=1_000_000_000,
            max_variants=2,
        )
        request = TypedRegionCompileRequest(
            self.capture.module,
            RuntimeFeedback(call_count=1, deopt_count=0),
            runtime_guard=self.capture.runtime_guard,
        )

        first = compiler.compile(request)
        second = compiler.compile(request)

        self.assertEqual(first.status, CompileStatus.COMPILED)
        self.assertEqual(second.status, CompileStatus.COMPILED)
        self.assertEqual(backend.calls, 1)
        self.assertIs(first.variant.jit_function, second.variant.jit_function)

    def test_code_replacement_misses_before_and_after_positive_cache_hit(self) -> None:
        backend = _Backend()
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=1,
            negative_ttl_ns=1_000_000_000,
        )
        request = TypedRegionCompileRequest(
            self.capture.module,
            RuntimeFeedback(call_count=1, deopt_count=0),
            runtime_guard=self.capture.runtime_guard,
        )
        compiled = compiler.compile(request)
        self.assertEqual(compiled.status, CompileStatus.COMPILED)
        self.assertIsNotNone(compiled.variant)
        original_code = generator_ratio.__code__
        try:
            generator_ratio.__code__ = replacement_generator_ratio.__code__
            rejected = compiler.compile(request)

            self.assertEqual(rejected.status, CompileStatus.UNSUPPORTED)
            self.assertEqual(
                rejected.reason_code,
                "runtime_dependency_guard_miss",
            )
            self.assertEqual(backend.calls, 1)
            with self.assertRaisesRegex(
                TypedGuardMiss,
                "runtime_dependency_changed",
            ):
                compiled.variant("abc-")
        finally:
            generator_ratio.__code__ = original_code

    def test_runtime_dependency_guard_is_required_and_rechecked_on_call(self) -> None:
        compiler = TypedRegionCompiler(
            _Backend(),
            call_threshold=1,
            negative_ttl_ns=1_000_000_000,
        )
        missing = compiler.compile(
            TypedRegionCompileRequest(
                self.capture.module,
                RuntimeFeedback(call_count=1, deopt_count=0),
            )
        )
        valid = compiler.compile(
            TypedRegionCompileRequest(
                self.capture.module,
                RuntimeFeedback(call_count=1, deopt_count=0),
                runtime_guard=self.capture.runtime_guard,
            )
        )

        self.assertEqual(missing.status, CompileStatus.UNSUPPORTED)
        self.assertEqual(missing.reason_code, "runtime_dependency_guard_missing")
        original = generator_ratio.__defaults__
        try:
            generator_ratio.__defaults__ = (0.9,)
            with self.assertRaisesRegex(
                TypedGuardMiss,
                "runtime_dependency_changed",
            ):
                valid.variant("abc-")
        finally:
            generator_ratio.__defaults__ = original

    def test_deopt_backoff_bypasses_a_positive_cache_entry(self) -> None:
        backend = _Backend()
        compiler = TypedRegionCompiler(
            backend,
            call_threshold=1,
            negative_ttl_ns=1_000_000_000,
            max_deopts=0,
        )
        compiled = compiler.compile(
            TypedRegionCompileRequest(
                self.capture.module,
                RuntimeFeedback(call_count=1, deopt_count=0),
                runtime_guard=self.capture.runtime_guard,
            )
        )
        backed_off = compiler.compile(
            TypedRegionCompileRequest(
                self.capture.module,
                RuntimeFeedback(call_count=2, deopt_count=1),
                runtime_guard=self.capture.runtime_guard,
            )
        )

        self.assertEqual(compiled.status, CompileStatus.COMPILED)
        self.assertEqual(backed_off.status, CompileStatus.DEFERRED)
        self.assertEqual(backed_off.reason_code, "runtime_deopt_backoff")
        self.assertEqual(backend.calls, 1)

    def test_diagnostics_off_does_not_import_the_worker_runtime(self) -> None:
        repository = Path(__file__).resolve().parents[4]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository / "src")
        environment["UDFJIT_DIAGNOSTICS"] = "off"
        program = """
import sys

from python_udf_jit.compiler.typed_frontend import capture_typed_loop
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
from python_udf_jit.provider.scalar_python.typed_loop import (
    BackendCompilation,
    RuntimeFeedback,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
)

def udf(text: str) -> int:
    return sum(1 for character in text if character.isalpha())

class Backend:
    adapter_version = "off-path-test"

    def compile(self, lowering):
        return BackendCompilation(True, "test")

capture = capture_typed_loop(udf, input_types=(EXACT_UNICODE,))
decision = TypedRegionCompiler(
    Backend(),
    call_threshold=1,
    negative_ttl_ns=1,
).compile(
    TypedRegionCompileRequest(
        capture.module,
        RuntimeFeedback(call_count=1, deopt_count=0),
        runtime_guard=capture.runtime_guard,
    )
)
assert decision.variant("Ab-") == 2
assert "python_udf_jit.diagnostics.worker_runtime" not in sys.modules
"""

        with tempfile.TemporaryDirectory() as directory:
            program_path = Path(directory) / "diagnostics_off_probe.py"
            program_path.write_text(program, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(program_path)],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


class CinderXTypedLoopBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lowering = lower_typed_loop(
            capture_typed_loop(
                alpha_count,
                input_types=(EXACT_UNICODE,),
            ).module
        )

    @staticmethod
    def _modules(jit_module: ModuleType) -> dict[str, ModuleType]:
        cinderx_module = ModuleType("cinderx")
        cinderx_module.jit = jit_module
        return {"cinderx": cinderx_module, "cinderx.jit": jit_module}

    def test_unicode_helper_requires_the_specialized_hir_opcode(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        jit_module._udf_unicode_count_property = lambda text, _property: sum(
            character.isalpha() for character in text
        )
        jit_module.force_compile = lambda _function: True
        jit_module.get_function_hir_opcode_counts = (
            lambda _function: {"UnicodeCountProperty": 1}
        )
        jit_module.is_jit_compiled = lambda _function: True

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertTrue(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_unicode_property_hir")
        self.assertIsNotNone(result.physical_lowering)

    def test_generic_typed_entry_is_used_when_physical_helper_is_absent(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        jit_module.compile_typed_region = lambda _function, _plan: True
        jit_module.get_function_hir_opcode_counts = lambda _function: {"Phi": 2}
        jit_module.is_jit_compiled = lambda _function: True

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertTrue(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_typed_hir_adapter")
        self.assertIsNone(result.physical_lowering)

    def test_backend_rejection_is_reported_without_claiming_jit_success(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        jit_module.compile_typed_region = lambda _function, _plan: False

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertFalse(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_typed_hir_adapter")


if __name__ == "__main__":
    unittest.main()
