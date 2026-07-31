from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from python_udf_jit.diagnostics.bundle import (
    BundleRunContext,
    open_bundle,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.hotspots import NormalizedPerfProfile
from python_udf_jit.diagnostics.report import (
    diff_diagnostic_bundles,
    hotspots_diagnostic_bundle,
    trace_diagnostic_bundle,
    validate_diagnostic_bundle,
)
from tests.unit.diagnostics.test_hotspots import _profile, _provenance


def build_diagnostic_bundle(
    root: Path,
    *,
    run_id: str,
    second_period: int = 6,
    extra_payload: bytes | None = None,
    include_perf: bool = True,
) -> Path:
    policy = resolve_diagnostic_policy(
        {
            "UDFJIT_DIAGNOSTICS": "full",
            "UDFJIT_DIAGNOSTIC_DIR": str(root / "bundles"),
            "UDFJIT_DIAGNOSTIC_FILTER": "artifact:abc123",
            "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
            "UDFJIT_DIAGNOSTIC_PERF": "record",
            "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
            "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(4 * 1024 * 1024),
        },
        DiagnosticRuntimeContext(
            workspace_root=root / "workspace",
            home_root=root / "home",
            dedicated_worker=True,
        ),
    )
    writer = open_bundle(
        policy,
        BundleRunContext(
            run_id=run_id,
            runtime_mode="auto",
            process_key="worker-1",
        ),
    )
    writer.add(
        "provenance/map.json",
        "application/json",
        _provenance().canonical_bytes(),
        {"layer": "provenance"},
    )
    if include_perf:
        profile_document = _profile(second_period=second_period).to_document()
        profile_document["run_id"] = run_id
        profile = NormalizedPerfProfile.from_document(profile_document)
        writer.add(
            "perf/samples.json",
            "application/json",
            json.dumps(
                profile.to_document(),
                sort_keys=True,
                separators=(",", ":"),
            ),
            {"layer": "perf"},
        )
    if extra_payload is not None:
        writer.add(
            "semantic/core.final.json",
            "application/json",
            extra_payload,
            {"layer": "semantic"},
        )
    return writer.complete().path


class DiagnosticReportTests(unittest.TestCase):
    def test_trace_only_requires_provenance_not_perf_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = build_diagnostic_bundle(
                Path(directory),
                run_id="run-a",
                include_perf=False,
            )

            validation = validate_diagnostic_bundle(bundle)
            trace = trace_diagnostic_bundle(
                bundle,
                "machine:c:0",
                direction="upstream",
            )

        self.assertEqual(validation["status"], "valid")
        self.assertEqual(trace["node"]["node_id"], "machine:c:0")

    def test_validate_trace_and_hotspots_are_bounded_data_only_queries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = build_diagnostic_bundle(
                Path(directory),
                run_id="run-a",
            )

            validation = validate_diagnostic_bundle(bundle)
            trace = trace_diagnostic_bundle(
                bundle,
                "machine:c:1",
                direction="upstream",
            )
            hotspots = hotspots_diagnostic_bundle(
                bundle,
                group_by="source",
            )

        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["bundle_status"], "complete")
        self.assertEqual(validation["artifact_count"], 2)
        self.assertFalse(validation["executed_content"])
        self.assertEqual(trace["node"]["node_id"], "machine:c:1")
        self.assertEqual(
            {node["node_id"] for node in trace["upstream"]},
            {
                "source:f:10:0:10:4",
                "source:f:11:0:11:4",
                "genbc:g:2",
                "hir:c:1",
                "lir:c:1",
            },
        )
        self.assertNotIn("downstream", trace)
        self.assertEqual(hotspots["results"]["group_by"], "source")
        self.assertEqual(hotspots["coverage"], 0.8)
        self.assertFalse(hotspots["executed_content"])

    def test_diff_compares_artifact_hashes_and_projected_hotspots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = build_diagnostic_bundle(
                root,
                run_id="run-a",
                extra_payload=b'{"version":1}',
            )
            candidate = build_diagnostic_bundle(
                root,
                run_id="run-b",
                second_period=12,
                extra_payload=b'{"version":2}',
            )

            difference = diff_diagnostic_bundles(
                baseline,
                candidate,
                group_by="source",
            )

        self.assertEqual(difference["status"], "valid")
        self.assertEqual(
            difference["results"]["hotspots"]["total_weight_delta"],
            6,
        )
        changed = {
            item["path"]: item
            for item in difference["results"]["artifacts"]
        }
        self.assertEqual(
            changed["semantic/core.final.json"]["change"],
            "modified",
        )
        self.assertNotEqual(
            changed["semantic/core.final.json"]["baseline_sha256"],
            changed["semantic/core.final.json"]["candidate_sha256"],
        )
        self.assertFalse(difference["executed_content"])


if __name__ == "__main__":
    unittest.main()
