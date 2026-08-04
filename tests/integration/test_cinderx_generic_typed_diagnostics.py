from __future__ import annotations

import dis
import os
import unittest

from python_udf_jit.compiler.typed_frontend import capture_typed_loop
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
from python_udf_jit.diagnostics.cinderx_bridge import (
    CinderXDiagnosticStatus,
    collect_cinderx_compilation_diagnostics,
)
from python_udf_jit.provider.scalar_python.typed_loop import (
    CinderXTypedLoopBackend,
    lower_typed_loop,
)


def _alpha_count(text: str) -> int:
    return sum(1 for character in text if character.isalpha())


class _DiagnosticSink:
    def prepare_typed_compilation(
        self,
        function,
        generated_code_hash: str,
        operation_lines: tuple[tuple[str, int], ...],
    ) -> str:
        function.__udfjit_generated_code_hash__ = generated_code_hash
        function.__udfjit_typed_operation_lines__ = operation_lines
        return f"typed-{generated_code_hash[:24]}"


class GenericTypedDiagnosticsTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("PYTHONJITUDFDIAGNOSTICS"),
        "requires a dedicated CinderX diagnostics worker",
    )
    def test_generic_typed_region_preserves_hir_lir_machine_origins(
        self,
    ) -> None:
        import cinderx
        import cinderx.jit as jit

        cinderx.init()

        captured = capture_typed_loop(
            _alpha_count,
            input_types=(EXACT_UNICODE,),
        )
        lowering = lower_typed_loop(captured.module)
        backend = CinderXTypedLoopBackend().compile_with_diagnostics(
            lowering,
            _DiagnosticSink(),
        )

        self.assertTrue(backend.jit_compiled)
        counts = dict(backend.hir_opcode_counts)
        self.assertGreaterEqual(counts.get("Phi", 0), 2)
        self.assertGreaterEqual(counts.get("CondBranch", 0), 1)
        self.assertGreaterEqual(counts.get("UnicodeRead", 0), 1)
        self.assertGreaterEqual(counts.get("UnicodeClassify", 0), 1)
        self.assertEqual(counts.get("VectorCall", 0), 0)
        self.assertEqual(counts.get("UnicodeCountProperty", 0), 0)

        compile_instance_id = f"typed-{lowering.generated_code_hash[:24]}"
        diagnostics = collect_cinderx_compilation_diagnostics(
            jit,
            lowering.function,
            compile_instance_id=compile_instance_id,
            generated_code_hash=lowering.generated_code_hash,
        )

        self.assertIs(
            diagnostics.status,
            CinderXDiagnosticStatus.AVAILABLE,
        )
        self.assertTrue(diagnostics.hir_nodes)
        self.assertTrue(diagnostics.lir_nodes)
        self.assertTrue(diagnostics.machine_ranges)
        operation_lines = set(dict(lowering.operation_lines).values())
        operation_offsets = {
            instruction.offset
            for instruction in dis.get_instructions(lowering.function)
            if instruction.positions is not None
            and instruction.positions.lineno in operation_lines
        }
        hir_offsets = {
            node.bytecode_offset
            for node in diagnostics.hir_nodes
            if node.bytecode_offset is not None
        }
        self.assertTrue(operation_offsets & hir_offsets)
