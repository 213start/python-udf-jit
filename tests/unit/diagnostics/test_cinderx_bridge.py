from __future__ import annotations

import json
import unittest

from python_udf_jit.diagnostics.cinderx_bridge import (
    CinderXCompilationDiagnostics,
    build_cinderx_artifacts,
    collect_cinderx_compilation_diagnostics,
    extend_provenance_with_cinderx,
)
from python_udf_jit.diagnostics.provenance import (
    PROVENANCE_MAP_VERSION,
    ProvenanceLayer,
    ProvenanceMap,
    ProvenanceNode,
)


_CODE_HASH = "a" * 64


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "available",
        "compile_instance_id": "compile-7",
        "generated_code_hash": _CODE_HASH,
        "jit_compiled": True,
        "unavailable_reason": None,
        "jit_gate_reason": None,
        "code_start": 4096,
        "code_size": 64,
        "stack_size": 32,
        "spill_stack_size": 8,
        "pass_timings": [
            {
                "name": "hir.simplify",
                "ordinal": 0,
                "duration_ns": 123,
            }
        ],
        "hir_nodes": [
            {
                "hir_id": "0",
                "opcode": "LoadArg",
                "bytecode_offset": 0,
                "synthetic_kind": None,
            },
            {
                "hir_id": "1",
                "opcode": "Return",
                "bytecode_offset": 2,
                "synthetic_kind": None,
            },
        ],
        "lir_nodes": [
            {
                "lir_id": "7",
                "opcode": "Load",
                "hir_ids": ["0"],
                "synthetic_kind": None,
            },
            {
                "lir_id": "8",
                "opcode": "Ret",
                "hir_ids": ["1"],
                "synthetic_kind": None,
            },
        ],
        "machine_ranges": [
            {
                "range_id": "0",
                "start": 4096,
                "end": 4128,
                "section": "hot",
                "symbol": "secret_function_name",
                "lir_ids": ["7"],
                "hir_ids": ["0"],
                "synthetic_kind": None,
            },
            {
                "range_id": "1",
                "start": 4128,
                "end": 4160,
                "section": "hot",
                "symbol": "secret_function_name",
                "lir_ids": ["8"],
                "hir_ids": ["1"],
                "synthetic_kind": None,
            },
        ],
        "deopt_metadata": [
            {
                "bytecode_offset": 2,
                "reason_code": "guard_failure",
            }
        ],
    }


def _generated_provenance() -> ProvenanceMap:
    return ProvenanceMap(
        PROVENANCE_MAP_VERSION,
        (
            ProvenanceNode(
                f"genbc:{_CODE_HASH}:0",
                ProvenanceLayer.GENERATED_BYTECODE,
                "LOAD_FAST",
                bytecode_offset=0,
            ),
            ProvenanceNode(
                f"genbc:{_CODE_HASH}:2",
                ProvenanceLayer.GENERATED_BYTECODE,
                "RETURN_VALUE",
                bytecode_offset=2,
            ),
        ),
        (),
    )


class CinderXBridgeTest(unittest.TestCase):
    def test_valid_payload_round_trips_and_projects_to_machine_ranges(self):
        diagnostics = CinderXCompilationDiagnostics.from_document(
            _document()
        )
        provenance = extend_provenance_with_cinderx(
            _generated_provenance(),
            diagnostics,
        )

        machine = [
            node
            for node in provenance.nodes
            if node.layer is ProvenanceLayer.MACHINE
        ]
        self.assertEqual(
            [(node.address_start, node.address_end) for node in machine],
            [(4096, 4128), (4128, 4160)],
        )
        generated = f"genbc:{_CODE_HASH}:0"
        downstream_layers = {
            node.layer
            for node in provenance.trace_downstream(generated)
        }
        self.assertEqual(
            downstream_layers,
            {
                ProvenanceLayer.HIR,
                ProvenanceLayer.LIR,
                ProvenanceLayer.MACHINE,
            },
        )
        self.assertEqual(
            CinderXCompilationDiagnostics.from_document(
                diagnostics.to_document()
            ),
            diagnostics,
        )

    def test_strict_payload_rejects_bad_ranges_and_dangling_origins(self):
        cases = []
        outside = _document()
        outside["machine_ranges"][0]["start"] = 4095
        cases.append(outside)
        overlap = _document()
        overlap["machine_ranges"][1]["start"] = 4127
        cases.append(overlap)
        dangling = _document()
        dangling["lir_nodes"][0]["hir_ids"] = ["missing"]
        cases.append(dangling)
        bad_offset = _document()
        bad_offset["hir_nodes"][0]["bytecode_offset"] = 1
        cases.append(bad_offset)

        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    CinderXCompilationDiagnostics.from_document(document)

    def test_available_payload_requires_every_structural_layer(self):
        for missing in ("hir_nodes", "lir_nodes", "machine_ranges"):
            document = _document()
            document[missing] = []
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    CinderXCompilationDiagnostics.from_document(document)

    def test_missing_structured_api_is_explicitly_unavailable(self):
        class ExistingJitWithoutStructuredApi:
            def print_hir(self, _function):
                raise AssertionError("text dump fallback must not be parsed")

        diagnostics = collect_cinderx_compilation_diagnostics(
            ExistingJitWithoutStructuredApi(),
            lambda value: value,
            compile_instance_id="compile-8",
            generated_code_hash=_CODE_HASH,
        )

        self.assertEqual(diagnostics.status, "backend_unavailable")
        self.assertFalse(diagnostics.jit_compiled)
        self.assertEqual(
            diagnostics.unavailable_reason,
            "structured_api_unavailable",
        )

    def test_collection_validates_identity_and_does_not_serialize_symbols(self):
        class StructuredJit:
            def get_udfjit_compilation_diagnostics(
                self,
                _function,
                compile_instance_id,
            ):
                document = _document()
                document["compile_instance_id"] = compile_instance_id
                return document

        diagnostics = collect_cinderx_compilation_diagnostics(
            StructuredJit(),
            lambda value: value,
            compile_instance_id="compile-7",
            generated_code_hash=_CODE_HASH,
        )
        artifacts = build_cinderx_artifacts(diagnostics)
        encoded = json.dumps(
            {
                "diagnostics": diagnostics.to_document(),
                "hir": artifacts.hir_text,
                "lir": artifacts.lir_text,
                "machine": artifacts.machine_ranges_text,
            },
            sort_keys=True,
        )

        self.assertNotIn("secret_function_name", encoded)
        self.assertIn('"symbol_sha256"', encoded)
        self.assertIn("hir:compile-7:0", artifacts.hir_text)
        self.assertIn("lir:compile-7:7", artifacts.lir_text)

    def test_structured_api_failure_becomes_sanitized_unavailable_result(self):
        class BrokenJit:
            def get_udfjit_compilation_diagnostics(self, _function, _identity):
                raise RuntimeError("customer value must not escape")

        diagnostics = collect_cinderx_compilation_diagnostics(
            BrokenJit(),
            lambda value: value,
            compile_instance_id="compile-9",
            generated_code_hash=_CODE_HASH,
        )

        self.assertEqual(diagnostics.status, "backend_unavailable")
        self.assertEqual(diagnostics.unavailable_reason, "structured_api_error")
        self.assertNotIn(
            "customer value",
            json.dumps(diagnostics.to_document()),
        )


if __name__ == "__main__":
    unittest.main()
