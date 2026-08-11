from __future__ import annotations

import functools
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.capture import (
    CaptureRequest,
    capture_program_request,
)
from python_udf_jit.compiler.identity import code_identity
from python_udf_jit.diagnostics.bundle import (
    BundleStatus,
    read_artifact_bytes,
    read_bundle,
    read_json_artifact,
)
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.events import clear_events, snapshot_events
from python_udf_jit.diagnostics.provenance import ProvenanceMap
from python_udf_jit.diagnostics.report import validate_diagnostic_bundle
from python_udf_jit.diagnostics.driver_runtime import (
    DriverDiagnosticRecorder,
    DriverDiagnosticStatus,
    DriverRejection,
)
from python_udf_jit.diagnostics.session import DiagnosticSession
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry


MANIFEST_SHA256 = "a" * 64


def string_clean(value: str) -> str:
    return value.strip()


def int_identity(value: int) -> int:
    return value


def opaque_float(value: float) -> float:
    print(value)
    return value + 1.0


def affine_float(value: float) -> float:
    return value * 2.0 + 1.0


def privacy_canary_float(value: float) -> float:
    return value + 918273.456789


@functools.wraps(string_clean)
def daft_string_method(_instance: object, value: str) -> str:
    return string_clean(value)


@functools.wraps(int_identity)
def daft_int_method(_instance: object, value: int) -> int:
    return int_identity(value)


class _FakeFunc:
    def __init__(self) -> None:
        self._method = daft_string_method


class _FakeIntFunc:
    def __init__(self) -> None:
        self._method = daft_int_method
        self.return_dtype = "int64"


class _FakePyExpr:
    def _hash(self) -> int:
        return id(self)

    def to_field(self, schema):
        return _FakeField(next(iter(schema.values())))


class _FakeField:
    def __init__(self, dtype) -> None:
        self._dtype = dtype

    def dtype(self):
        return self._dtype


class _FakeExpression:
    def __init__(self) -> None:
        self._expr = _FakePyExpr()


class _StringDataFrame:
    def schema(self):
        return {"text": "string"}


class _FloatDataFrame:
    def schema(self):
        return {"value": "float64"}


class _IntDataFrame:
    def schema(self):
        return {"value": "int64"}


class DriverDiagnosticBindingTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_events()

    @staticmethod
    def _full_policy(
        root: Path,
        function,
        *,
        source: str = "ranges",
        selector: str | None = None,
    ):
        code_sha256 = code_identity(function).sha256
        return resolve_diagnostic_policy(
            {
                "UDFJIT_DIAGNOSTICS": "full",
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": (
                    selector or f"udf:{code_sha256[:16]}"
                ),
                "UDFJIT_DIAGNOSTIC_SOURCE": source,
                "UDFJIT_DIAGNOSTIC_PERF": "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            },
            DiagnosticRuntimeContext(
                dedicated_worker=True,
                workspace_root=root / "workspace",
                home_root=root / "home",
            ),
        )

    @staticmethod
    def _recorder(policy) -> DriverDiagnosticRecorder:
        return DriverDiagnosticRecorder(
            policy,
            run_id="run-a",
            runtime_mode="auto",
            process_key="driver-1",
        )

    def test_summary_policy_does_not_create_driver_bundle_recorder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "summary",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
                },
                DiagnosticRuntimeContext(workspace_root=root / "workspace"),
            )

            registry = CandidateRegistry(
                MANIFEST_SHA256,
                diagnostic_policy=policy,
            )

            self.assertIsNone(registry._driver_diagnostics)

    def test_schema_rejection_emits_traceable_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code_sha256 = code_identity(string_clean).sha256
            policy = resolve_diagnostic_policy(
                {
                    "UDFJIT_DIAGNOSTICS": "full",
                    "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                    "UDFJIT_DIAGNOSTIC_FILTER": (
                        f"udf:{code_sha256[:16]}"
                    ),
                    "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                    "UDFJIT_DIAGNOSTIC_PERF": "off",
                    "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                    "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
                },
                DiagnosticRuntimeContext(
                    dedicated_worker=True,
                    workspace_root=root / "workspace",
                    home_root=root / "home",
                ),
            )
            registry = CandidateRegistry(
                MANIFEST_SHA256,
                job_namespace="driver-diagnostic-test",
                diagnostic_policy=policy,
                diagnostic_run_id="run-a",
                diagnostic_runtime_mode="auto",
                diagnostic_process_key="driver-1",
            )
            function = _FakeFunc()
            expression = _FakeExpression()
            record = registry.register(function, function._method)
            registry.bind_expression(
                expression,
                record,
                invocation_args=(),
            )

            finalized = registry.finalize_operation(
                _StringDataFrame(),
                "with_columns",
                ({"text": expression},),
                {},
            )

            self.assertEqual(finalized, 1)
            self.assertEqual(
                record.wrapper.carrier.diagnostic_policy_sha256,
                policy.sha256,
            )
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            bundle = read_bundle(bundles[0])
            self.assertIs(bundle.status, BundleStatus.PARTIAL)
            validation = validate_diagnostic_bundle(bundles[0])
            self.assertEqual(validation["status"], "valid")
            self.assertEqual(validation["bundle_status"], "partial")
            self.assertFalse(validation["executed_content"])
            paths = {artifact.path for artifact in bundle.artifacts}
            self.assertTrue(
                {
                    "source/identity.json",
                    "source/ranges.json",
                    "candidate/signature.json",
                    "bytecode/original.json",
                    "bytecode/original.dis",
                    "capture/result.json",
                    "reports/chain-status.json",
                    "reports/stages.json",
                    "provenance/map.json",
                    "provenance/nodes.json",
                    "provenance/edges.json",
                }
                <= paths
            )
            capture_result = read_json_artifact(
                bundle,
                "capture/result.json",
            )
            self.assertEqual(capture_result["stage"], "adapter")
            self.assertEqual(
                capture_result["reason_code"],
                "candidate_layout_unavailable",
            )
            chain = read_json_artifact(
                bundle,
                "reports/chain-status.json",
            )
            self.assertEqual(chain["source"], "available")
            self.assertEqual(chain["original_bytecode"], "available")
            self.assertEqual(
                chain["capture"]["unavailable_reason"],
                "candidate_layout_unavailable",
            )
            self.assertEqual(
                chain["machine"]["unavailable_reason"],
                "capture_rejected",
            )
            provenance = ProvenanceMap.from_document(
                read_json_artifact(bundle, "provenance/map.json")
            )
            self.assertTrue(
                any(node.layer.value == "source" for node in provenance.nodes)
            )
            self.assertTrue(
                any(
                    node.layer.value == "original_bytecode"
                    for node in provenance.nodes
                )
            )
            self.assertNotIn("source/source.py", paths)

    def test_provider_ineligible_layout_reports_provider_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, int_identity)
            registry = CandidateRegistry(
                MANIFEST_SHA256,
                diagnostic_policy=policy,
                diagnostic_run_id="run-a",
                diagnostic_runtime_mode="auto",
                diagnostic_process_key="driver-1",
            )
            function = _FakeIntFunc()
            expression = _FakeExpression()
            record = registry.register(function, function._method)
            registry.bind_expression(
                expression,
                record,
                invocation_args=(expression,),
            )

            finalized = registry.finalize_operation(
                _IntDataFrame(),
                "with_columns",
                ({"value": expression},),
                {},
            )

            self.assertEqual(finalized, 1)
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            capture_result = read_json_artifact(
                read_bundle(bundles[0]),
                "capture/result.json",
            )
            self.assertEqual(capture_result["stage"], "adapter")
            self.assertEqual(
                capture_result["reason_code"],
                "candidate_layout_provider_unavailable",
            )

    def test_semantic_rejection_retains_capture_and_cfg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, opaque_float)
            program = capture_program_request(
                CaptureRequest(opaque_float)
            )
            self.assertIsInstance(program, CapturedProgram)

            outcome = self._recorder(policy).record_rejection(
                candidate_id="candidate-a",
                callable_object=opaque_float,
                original_callable=opaque_float,
                logical_schema="float64",
                usage_context="projection",
                rejection=DriverRejection(
                    "semantic",
                    "semantic_pipeline_not_scalar_eligible",
                ),
                captured_program=program,
            )

            self.assertIs(outcome.status, DriverDiagnosticStatus.RECORDED)
            self.assertIsNotNone(outcome.bundle)
            bundle = read_bundle(outcome.bundle.path)
            paths = {artifact.path for artifact in bundle.artifacts}
            self.assertIn("capture/capture.json", paths)
            self.assertIn("capture/cfg.json", paths)
            capture_result = read_json_artifact(
                bundle,
                "capture/result.json",
            )
            self.assertTrue(capture_result["admitted"])
            chain = read_json_artifact(
                bundle,
                "reports/chain-status.json",
            )
            self.assertEqual(chain["capture"]["status"], "available")
            self.assertEqual(chain["semantic"]["status"], "rejected")

    def test_ranges_policy_redacts_capture_scalar_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, privacy_canary_float)
            program = capture_program_request(
                CaptureRequest(privacy_canary_float)
            )
            self.assertIsInstance(program, CapturedProgram)

            outcome = self._recorder(policy).record_rejection(
                candidate_id="candidate-a",
                callable_object=privacy_canary_float,
                original_callable=privacy_canary_float,
                logical_schema="float64",
                usage_context="projection",
                rejection=DriverRejection(
                    "semantic",
                    "semantic_pipeline_not_scalar_eligible",
                ),
                captured_program=program,
            )

            self.assertIs(outcome.status, DriverDiagnosticStatus.RECORDED)
            self.assertIsNotNone(outcome.bundle)
            bundle = read_bundle(outcome.bundle.path)
            capture = read_json_artifact(bundle, "capture/capture.json")
            cfg = read_json_artifact(bundle, "capture/cfg.json")
            raw_decimal = "918273.456789"
            raw_hex = (918273.456789).hex()
            redacted_constants = [
                value
                for value in capture["scalar_constants"]
                if value is not None
            ]

            self.assertEqual(
                redacted_constants,
                [
                    {
                        "kind": "float",
                        "sha256": hashlib.sha256(
                            raw_hex.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
            )
            self.assertEqual(
                capture["frontend"]["control_flow_graph"],
                cfg,
            )
            self.assertTrue(cfg["blocks"])
            self.assertIn(
                cfg["entry_block"],
                {block["block_id"] for block in cfg["blocks"]},
            )
            for artifact in bundle.artifacts:
                if not (
                    artifact.media_type == "application/json"
                    or artifact.media_type.endswith("+json")
                    or artifact.media_type.startswith("text/")
                ):
                    continue
                payload = read_artifact_bytes(bundle, artifact.path).decode(
                    "utf-8"
                )
                self.assertNotIn(raw_decimal, payload, artifact.path)
                self.assertNotIn(raw_hex, payload, artifact.path)

    def test_registry_classifies_artifact_failure_after_semantic_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, affine_float)
            registry = CandidateRegistry(
                MANIFEST_SHA256,
                diagnostic_policy=policy,
                diagnostic_run_id="run-a",
                diagnostic_runtime_mode="auto",
                diagnostic_process_key="driver-1",
            )
            expression = _FakeExpression()
            record = registry.register(affine_float, affine_float)
            affine_float.return_dtype = "float64"
            registry.bind_expression(
                expression,
                record,
                invocation_args=(expression,),
            )

            try:
                with mock.patch(
                    "python_udf_jit.integration.daft_ray.registry.encode_artifact",
                    side_effect=RuntimeError("encoding_failed"),
                ):
                    finalized = registry.finalize_operation(
                        _FloatDataFrame(),
                        "with_columns",
                        ({"value": expression},),
                        {},
                    )
            finally:
                del affine_float.return_dtype

            self.assertEqual(finalized, 1)
            self.assertEqual(registry.diagnostic_failure_count, 0)
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            bundle = read_bundle(bundles[0])
            capture_result = read_json_artifact(
                bundle,
                "capture/result.json",
            )
            self.assertEqual(capture_result["stage"], "artifact")
            self.assertEqual(
                capture_result["reason_code"],
                "artifact_encoding_failed",
            )
            paths = {artifact.path for artifact in bundle.artifacts}
            self.assertIn("capture/capture.json", paths)
            self.assertIn("capture/cfg.json", paths)
            chain = read_json_artifact(
                bundle,
                "reports/chain-status.json",
            )
            self.assertEqual(chain["semantic"]["status"], "available")
            self.assertEqual(
                chain["machine"]["unavailable_reason"],
                "artifact_rejected",
            )

    def test_capture_and_artifact_rejections_report_exact_boundaries(self) -> None:
        program = capture_program_request(CaptureRequest(opaque_float))
        self.assertIsInstance(program, CapturedProgram)
        cases = (
            (
                "capture",
                "capture_verification_failed",
                None,
                "rejected",
                "unavailable",
            ),
            (
                "artifact",
                "artifact_encoding_failed",
                program,
                "available",
                "available",
            ),
        )
        for stage, reason, captured, capture_status, semantic_status in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                policy = self._full_policy(root, opaque_float)
                outcome = self._recorder(policy).record_rejection(
                    candidate_id="candidate-a",
                    callable_object=opaque_float,
                    original_callable=opaque_float,
                    logical_schema="float64",
                    usage_context="projection",
                    rejection=DriverRejection(stage, reason),
                    captured_program=captured,
                )

                self.assertIs(outcome.status, DriverDiagnosticStatus.RECORDED)
                self.assertIsNotNone(outcome.bundle)
                chain = read_json_artifact(
                    read_bundle(outcome.bundle.path),
                    "reports/chain-status.json",
                )
                self.assertEqual(chain["capture"]["status"], capture_status)
                self.assertEqual(chain["semantic"]["status"], semantic_status)
                if stage == "artifact":
                    self.assertIsNone(
                        chain["semantic"]["unavailable_reason"]
                    )
                    self.assertEqual(
                        chain["machine"]["unavailable_reason"],
                        "artifact_rejected",
                    )

    def test_source_text_requires_explicit_text_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, string_clean, source="text")

            outcome = self._recorder(policy).record_rejection(
                candidate_id="candidate-a",
                callable_object=string_clean,
                original_callable=string_clean,
                logical_schema="string",
                usage_context="projection",
                rejection=DriverRejection(
                    "adapter",
                    "logical_schema_not_float64",
                ),
            )

            self.assertIs(outcome.status, DriverDiagnosticStatus.RECORDED)
            self.assertIsNotNone(outcome.bundle)
            source = (outcome.bundle.path / "source/source.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("def string_clean", source)

    def test_required_artifact_failure_publishes_incomplete_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, string_clean)
            recorder = self._recorder(policy)

            with mock.patch.object(
                DiagnosticSession,
                "record_artifact",
                return_value=None,
            ):
                outcome = recorder.record_rejection(
                    candidate_id="candidate-a",
                    callable_object=string_clean,
                    original_callable=string_clean,
                    logical_schema="string",
                    usage_context="projection",
                    rejection=DriverRejection(
                        "adapter",
                        "logical_schema_not_float64",
                    ),
                )

            self.assertIs(outcome.status, DriverDiagnosticStatus.FAILED)
            self.assertEqual(recorder.failure_count, 1)
            self.assertIsNotNone(outcome.bundle)
            bundle = read_bundle(outcome.bundle.path)
            self.assertIs(bundle.status, BundleStatus.INCOMPLETE)
            self.assertNotIn(
                "reports/chain-status.json",
                {artifact.path for artifact in bundle.artifacts},
            )
            self.assertTrue(
                any(
                    event.reason_code
                    == "driver_required_artifact_missing"
                    for event in snapshot_events()
                )
            )

    def test_finalize_failure_is_observable_without_masking_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = self._full_policy(root, string_clean)
            recorder = self._recorder(policy)

            with mock.patch.object(
                DiagnosticSession,
                "finalize",
                side_effect=RuntimeError("disk_failure"),
            ):
                outcome = recorder.record_rejection(
                    candidate_id="candidate-a",
                    callable_object=string_clean,
                    original_callable=string_clean,
                    logical_schema="string",
                    usage_context="projection",
                    rejection=DriverRejection(
                        "adapter",
                        "logical_schema_not_float64",
                    ),
                )

            self.assertIs(outcome.status, DriverDiagnosticStatus.FAILED)
            self.assertIsNone(outcome.bundle)
            self.assertEqual(outcome.reason_code, "driver_recording_failed")
            self.assertEqual(recorder.failure_count, 1)
            self.assertTrue(
                any(
                    event.reason_code == "driver_recording_failed"
                    for event in snapshot_events()
                )
            )

    def test_driver_selectors_match_only_available_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate_policy = self._full_policy(
                root,
                string_clean,
                selector="candidate:candidate-a",
            )
            candidate = self._recorder(candidate_policy).record_rejection(
                candidate_id="candidate-a",
                callable_object=string_clean,
                original_callable=string_clean,
                logical_schema="string",
                usage_context="projection",
                rejection=DriverRejection(
                    "adapter",
                    "logical_schema_not_float64",
                ),
            )
            self.assertIs(candidate.status, DriverDiagnosticStatus.RECORDED)

        for kind in ("artifact", "region"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                policy = self._full_policy(
                    root,
                    string_clean,
                    selector=f"{kind}:unavailable-a",
                )
                outcome = self._recorder(policy).record_rejection(
                    candidate_id="candidate-a",
                    callable_object=string_clean,
                    original_callable=string_clean,
                    logical_schema="string",
                    usage_context="projection",
                    rejection=DriverRejection(
                        "adapter",
                        "logical_schema_not_float64",
                    ),
                )
                self.assertIs(
                    outcome.status,
                    DriverDiagnosticStatus.NOT_SELECTED,
                )
                self.assertFalse((root / "diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
