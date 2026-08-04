from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
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
)
from python_udf_jit.provider.scalar_python.typed_loop import (
    BackendCompilation,
    CinderXTypedLoopBackend,
    CompileStatus,
    RuntimeFeedback,
    TypedGuardMiss,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
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


def scalar_remap(text: str) -> str:
    table = str.maketrans({"α": "a", "β": "b", "→": ">"})
    return text.translate(table)


_SPACE_RUN = re.compile(r"\s+")


def collapse_space_runs(text: str) -> str:
    return _SPACE_RUN.sub(" ", text).strip()


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

    def test_sequence_transforms_share_generic_loop_and_type_lowering(self) -> None:
        cases = (scalar_remap, collapse_space_runs)
        for function in cases:
            with self.subTest(function=function.__name__):
                lowering = lower_typed_loop(
                    capture_typed_loop(
                        function,
                        input_types=(EXACT_UNICODE,),
                    ).module
                )

                self.assertEqual(lowering.plan.result_strategy, "sequence_builder")
                self.assertIn("sequence_builder", lowering.plan.backend_requirements)
                self.assertNotIn(function.__name__, lowering.generated_source)
                for text in ("", "  α\tβ  ", "x→y", "中文\u2003text"):
                    self.assertEqual(lowering.function(text), function(text))

    def test_canonical_bridge_exposes_only_generic_data_primitives(self) -> None:
        cases = (alpha_count, scalar_remap, collapse_space_runs)
        forbidden = {
            "_fsm_transition",
            "_immutable_unicode_lookup",
            "_sequence_builder_apply",
            "_sequence_builder_append",
            "unicode.count_property",
            "unicode.fsm_sequence",
            "unicode.map_sequence",
        }
        for function in cases:
            with self.subTest(function=function.__name__):
                lowering = lower_typed_loop(
                    capture_typed_loop(
                        function,
                        input_types=(EXACT_UNICODE,),
                    ).module
                )

                self.assertTrue(
                    {
                        "_typed_sequence_length",
                        "_typed_unicode_read",
                    }.issubset(lowering.function.__globals__)
                )
                self.assertFalse(
                    any(name in lowering.generated_source for name in forbidden)
                )
                for text in ("", "Ab-α→β", "  a\t\n中文\u2003"):
                    self.assertEqual(lowering.function(text), function(text))

    def test_fsm_bridge_materializes_generic_control_and_table_lookup(self) -> None:
        lowering = lower_typed_loop(
            capture_typed_loop(
                collapse_space_runs,
                input_types=(EXACT_UNICODE,),
            ).module
        )

        self.assertIn("_typed_table_get", lowering.generated_source)
        self.assertIn("_typed_builder_append", lowering.generated_source)
        self.assertIn("if ", lowering.generated_source)
        self.assertNotIn("_typed_builder_apply", lowering.generated_source)
        self.assertNotIn("_typed_fsm_transition", lowering.generated_source)


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

    def test_generic_typed_entry_owns_unicode_reduction_lowering(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        calls = []

        def compile_typed_region(function, semantic, plan):
            calls.append((function, semantic, plan))
            return True

        jit_module.compile_typed_region = compile_typed_region
        # Keeping the retired helper visible proves that the adapter no longer
        # selects the whole-algorithm shortcut when the generic entry exists.
        jit_module._udf_unicode_count_property = lambda *_args: self.fail(
            "whole-algorithm Unicode count helper must not be called"
        )
        jit_module.get_function_hir_opcode_counts = (
            lambda _function: {
                "Phi": 2,
                "CondBranch": 1,
                "IntBinaryOp": 2,
                "LoadField": 1,
                "UnicodeRead": 1,
                "UnicodeClassify": 1,
            }
        )
        jit_module.is_jit_compiled = lambda _function: True

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertTrue(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_generic_typed_hir")
        self.assertEqual(len(calls), 1)
        _, semantic, plan = calls[0]
        self.assertEqual(semantic["semantic_hash"], self.lowering.module_hash)
        self.assertEqual(plan["plan_hash"], self.lowering.plan.plan_hash)
        operations = {operation["op"] for operation in semantic["operations"]}
        self.assertIn("sequence.get", operations)
        self.assertIn("unicode.property", operations)
        self.assertNotIn("unicode.count_property", operations)

    def test_generic_typed_entry_does_not_prescribe_backend_hir_shape(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        jit_module.compile_typed_region = lambda _function, _semantic, _plan: True
        jit_module.get_function_hir_opcode_counts = lambda _function: {"Phi": 2}
        jit_module.is_jit_compiled = lambda _function: True

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertTrue(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_generic_typed_hir")
        self.assertEqual(result.hir_opcode_counts, (("Phi", 2),))

    def test_sequence_patterns_use_the_same_generic_hir_entry(self) -> None:
        cases = (
            (
                scalar_remap,
                {
                    "CondBranch": 1,
                    "IntBinaryOp": 1,
                    "Phi": 2,
                    "LoadField": 1,
                    "PrimitiveTableLookup": 1,
                    "SequenceBuilderAppend": 1,
                    "SequenceBuilderCreate": 1,
                    "SequenceBuilderFinish": 1,
                    "UnicodeRead": 1,
                },
            ),
            (
                collapse_space_runs,
                {
                    "CondBranch": 2,
                    "IntBinaryOp": 5,
                    "Phi": 3,
                    "LoadField": 1,
                    "PrimitiveTableGet": 3,
                    "SequenceBuilderAppend": 2,
                    "SequenceBuilderCreate": 1,
                    "SequenceBuilderFinish": 1,
                    "UnicodeRead": 1,
                    "UnicodeClassify": 1,
                },
            ),
        )
        for function, required_counts in cases:
            with self.subTest(function=function.__name__):
                lowering = lower_typed_loop(
                    capture_typed_loop(
                        function,
                        input_types=(EXACT_UNICODE,),
                    ).module
                )
                jit_module = ModuleType("cinderx.jit")
                documents = []
                jit_module.compile_typed_region = (
                    lambda _function, semantic, plan: documents.append(
                        (semantic, plan)
                    )
                    or True
                )
                jit_module.get_function_hir_opcode_counts = (
                    lambda _function, required_counts=required_counts: dict(
                        required_counts
                    )
                )
                jit_module.is_jit_compiled = lambda _function: True

                with mock.patch.dict(sys.modules, self._modules(jit_module)):
                    result = CinderXTypedLoopBackend().compile(lowering)

                self.assertTrue(result.jit_compiled)
                self.assertEqual(result.execution_mode, "cinderx_generic_typed_hir")
                self.assertEqual(len(documents), 1)
                semantic_operations = {
                    operation["op"]
                    for operation in documents[0][0]["operations"]
                }
                self.assertFalse(
                    semantic_operations
                    & {
                        "unicode.count_property",
                        "unicode.map_sequence",
                        "unicode.fsm_sequence",
                    }
                )

    def test_unseen_unicode_property_needs_no_adapter_change(self) -> None:
        lowering = lower_typed_loop(
            capture_typed_loop(
                space_count,
                input_types=(EXACT_UNICODE,),
            ).module
        )
        jit_module = ModuleType("cinderx.jit")
        documents = []
        jit_module.compile_typed_region = (
            lambda _function, semantic, plan: documents.append((semantic, plan))
            or True
        )
        jit_module.get_function_hir_opcode_counts = lambda _function: {
            "CondBranch": 1,
            "IntBinaryOp": 2,
            "Phi": 2,
            "LoadField": 1,
            "UnicodeRead": 1,
            "UnicodeClassify": 1,
        }
        jit_module.is_jit_compiled = lambda _function: True

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(lowering)

        self.assertTrue(result.jit_compiled)
        properties = {
            dict(operation["attributes"])["property"]
            for operation in documents[0][0]["operations"]
            if operation["op"] == "unicode.property"
        }
        self.assertEqual(properties, {"space"})

    def test_backend_rejection_is_reported_without_claiming_jit_success(self) -> None:
        jit_module = ModuleType("cinderx.jit")
        jit_module.compile_typed_region = lambda _function, _semantic, _plan: False

        with mock.patch.dict(sys.modules, self._modules(jit_module)):
            result = CinderXTypedLoopBackend().compile(self.lowering)

        self.assertFalse(result.jit_compiled)
        self.assertEqual(result.execution_mode, "cinderx_generic_typed_hir")


if __name__ == "__main__":
    unittest.main()
