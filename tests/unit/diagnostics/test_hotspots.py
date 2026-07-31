from __future__ import annotations

import json
import unittest
from dataclasses import replace

from python_udf_jit.compiler.source_map import SourcePosition
from python_udf_jit.diagnostics.hotspots import (
    NormalizedPerfProfile,
    diff_hotspot_reports,
    project_hotspots,
)
from python_udf_jit.diagnostics.provenance import (
    PROVENANCE_MAP_VERSION,
    ProvenanceEdge,
    ProvenanceLayer,
    ProvenanceMap,
    ProvenanceNode,
    ProvenanceRelation,
)


def _provenance() -> ProvenanceMap:
    nodes = (
        ProvenanceNode(
            "source:f:10:0:10:4",
            ProvenanceLayer.SOURCE,
            "source_range",
            source_position=SourcePosition(10, 10, 0, 4),
        ),
        ProvenanceNode(
            "source:f:11:0:11:4",
            ProvenanceLayer.SOURCE,
            "source_range",
            source_position=SourcePosition(11, 11, 0, 4),
        ),
        ProvenanceNode(
            "genbc:g:0",
            ProvenanceLayer.GENERATED_BYTECODE,
            "LOAD_FAST",
            bytecode_offset=0,
        ),
        ProvenanceNode(
            "genbc:g:2",
            ProvenanceLayer.GENERATED_BYTECODE,
            "BINARY_OP",
            bytecode_offset=2,
        ),
        ProvenanceNode("hir:c:0", ProvenanceLayer.HIR, "LoadArg"),
        ProvenanceNode("hir:c:1", ProvenanceLayer.HIR, "BinaryOp"),
        ProvenanceNode("lir:c:0", ProvenanceLayer.LIR, "Load"),
        ProvenanceNode("lir:c:1", ProvenanceLayer.LIR, "Add"),
        ProvenanceNode(
            "machine:c:0",
            ProvenanceLayer.MACHINE,
            "machine_range",
            attributes=(("symbol_sha256", "a" * 64),),
            address_start=0x1000,
            address_end=0x1010,
        ),
        ProvenanceNode(
            "machine:c:1",
            ProvenanceLayer.MACHINE,
            "machine_range",
            attributes=(("symbol_sha256", "a" * 64),),
            address_start=0x1010,
            address_end=0x1020,
        ),
    )
    edges = (
        ProvenanceEdge(
            "source:f:10:0:10:4",
            "genbc:g:0",
            ProvenanceRelation.DERIVED,
        ),
        ProvenanceEdge(
            "source:f:10:0:10:4",
            "genbc:g:2",
            ProvenanceRelation.DERIVED,
        ),
        ProvenanceEdge(
            "source:f:11:0:11:4",
            "genbc:g:2",
            ProvenanceRelation.DERIVED,
        ),
        ProvenanceEdge(
            "genbc:g:0",
            "hir:c:0",
            ProvenanceRelation.LOWERED,
        ),
        ProvenanceEdge(
            "genbc:g:2",
            "hir:c:1",
            ProvenanceRelation.LOWERED,
        ),
        ProvenanceEdge(
            "hir:c:0",
            "lir:c:0",
            ProvenanceRelation.LOWERED,
        ),
        ProvenanceEdge(
            "hir:c:1",
            "lir:c:1",
            ProvenanceRelation.LOWERED,
        ),
        ProvenanceEdge(
            "lir:c:0",
            "machine:c:0",
            ProvenanceRelation.LOWERED,
        ),
        ProvenanceEdge(
            "lir:c:1",
            "machine:c:1",
            ProvenanceRelation.LOWERED,
        ),
    )
    return ProvenanceMap(PROVENANCE_MAP_VERSION, nodes, edges)


def _profile(*, second_period: int = 6) -> NormalizedPerfProfile:
    return NormalizedPerfProfile.from_document(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "process_id": 41,
            "event": "cycles",
            "lost_samples": 2,
            "samples": [
                {
                    "sample_id": "sample-0",
                    "pid": 41,
                    "tid": 41,
                    "timestamp_ns": 100,
                    "event": "cycles",
                    "ip": 0x1004,
                    "period": 10,
                    "runtime_phase": "execute",
                    "symbol": "private_symbol",
                },
                {
                    "sample_id": "sample-1",
                    "pid": 41,
                    "tid": 42,
                    "timestamp_ns": 110,
                    "event": "cycles",
                    "ip": 0x1014,
                    "period": second_period,
                    "runtime_phase": "execute",
                    "symbol_sha256": "a" * 64,
                },
                {
                    "sample_id": "sample-2",
                    "pid": 41,
                    "tid": 42,
                    "timestamp_ns": 120,
                    "event": "cycles",
                    "ip": 0x2000,
                    "period": 4,
                    "runtime_phase": "python_fallback",
                    "symbol_sha256": None,
                },
            ],
        }
    )


class HotspotProjectionTest(unittest.TestCase):
    def test_source_projection_splits_shared_origins_and_reports_coverage(self):
        report = project_hotspots(_profile(), _provenance(), "source")
        entries = {entry.key: entry for entry in report.entries}

        self.assertEqual(report.total_weight, 20)
        self.assertEqual(report.attributed_weight, 16)
        self.assertEqual(report.exact_weight, 10)
        self.assertEqual(report.shared_weight, 6)
        self.assertEqual(report.unattributed_weight, 4)
        self.assertEqual(report.coverage, 0.8)
        self.assertEqual(entries["source:f:10:0:10:4"].weight, 13.0)
        self.assertEqual(entries["source:f:11:0:11:4"].weight, 3.0)
        self.assertEqual(
            entries["source:f:10:0:10:4"].classification,
            "mixed",
        )

    def test_phase_and_symbol_projection_do_not_require_source_origins(self):
        phase = project_hotspots(_profile(), _provenance(), "phase")
        symbol = project_hotspots(_profile(), _provenance(), "symbol")

        self.assertEqual(phase.coverage, 1.0)
        self.assertEqual(
            {entry.key: entry.weight for entry in phase.entries},
            {"execute": 16.0, "python_fallback": 4.0},
        )
        self.assertEqual(symbol.attributed_weight, 16)
        self.assertEqual(symbol.entries[0].key, "a" * 64)
        self.assertNotIn(
            "private_symbol",
            json.dumps(_profile().to_document()),
        )

    def test_profile_rejects_cross_process_duplicate_and_invalid_samples(self):
        cases = []
        wrong_pid = _profile().to_document()
        wrong_pid["samples"][0]["pid"] = 99
        cases.append(wrong_pid)
        duplicate = _profile().to_document()
        duplicate["samples"][1]["sample_id"] = "sample-0"
        cases.append(duplicate)
        bad_period = _profile().to_document()
        bad_period["samples"][0]["period"] = 0
        cases.append(bad_period)
        bad_ip = _profile().to_document()
        bad_ip["samples"][0]["ip"] = 1 << 64
        cases.append(bad_ip)

        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    NormalizedPerfProfile.from_document(document)

    def test_symbol_projection_rejects_non_digest_machine_metadata(self) -> None:
        provenance = _provenance()
        nodes = tuple(
            replace(node, attributes=(("symbol_sha256", "private-name"),))
            if node.node_id == "machine:c:0"
            else node
            for node in provenance.nodes
        )

        with self.assertRaises(ValueError):
            project_hotspots(
                _profile(),
                ProvenanceMap(PROVENANCE_MAP_VERSION, nodes, provenance.edges),
                "symbol",
            )

    def test_hotspot_diff_is_keyed_and_preserves_coverage_delta(self):
        baseline = project_hotspots(_profile(), _provenance(), "source")
        candidate = project_hotspots(
            _profile(second_period=12),
            _provenance(),
            "source",
        )

        difference = diff_hotspot_reports(baseline, candidate)

        self.assertEqual(difference["group_by"], "source")
        self.assertEqual(difference["total_weight_delta"], 6)
        self.assertGreater(difference["coverage_delta"], 0)
        changed = {
            entry["key"]: entry["weight_delta"]
            for entry in difference["entries"]
        }
        self.assertEqual(changed["source:f:10:0:10:4"], 3.0)
        self.assertEqual(changed["source:f:11:0:11:4"], 3.0)


if __name__ == "__main__":
    unittest.main()
