from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import python_udf_jit.integration.daft_ray.worker as worker_runtime
from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.capture import (
    CaptureRequest,
    FallbackIdentity,
    capture,
)
from python_udf_jit.compiler.core_ir import rehash_semantic_module
from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.compiler.region import form_semantic_region_graph
from python_udf_jit.compiler.reference import reference_resume_semantic
from python_udf_jit.diagnostics.report import InMemoryRuntimeReport
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    OFF_DIAGNOSTIC_POLICY,
    resolve_diagnostic_policy,
)
from python_udf_jit.governance.policy import PolicySnapshot
from python_udf_jit.integration.daft_ray.carrier import (
    InlineArtifactHandle,
    ProductionCarrierState,
)
from python_udf_jit.integration.daft_ray.invocation_layout import (
    InvocationLayoutContract,
)
from python_udf_jit.integration.daft_ray.worker import (
    RuntimeTarget,
    WorkerGuardOverrides,
    WorkerRuntimeContext,
    WorkerScalarAdapter,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import (
    ARTIFACT_HEADER,
    ARTIFACT_MAGIC,
    SECTION_HEADER,
    encode_artifact,
)
from python_udf_jit.protocol.loader import ArtifactLoader
from python_udf_jit.protocol.manifest import (
    DEFAULT_MANIFEST,
    DependencyRequirement,
)
from python_udf_jit.provider.scalar_python.capability import (
    CapabilityError,
    CapabilityRegistry,
    CapabilityRejectCode,
)
from python_udf_jit.provider.scalar_python.compiler import (
    compile_semantic_scalar_region,
)
from python_udf_jit.provider.scalar_python.executor import (
    PreSemanticsExecutionError,
    ScalarExecutor,
    ScalarProviderVariant,
)
from python_udf_jit.runtime.continuation import build_continuation_payload
from python_udf_jit.runtime.layout import LocalScalarSlotBackend
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer
from python_udf_jit.runtime.variant import WorkerProcessKey


def affine(value):
    return value * 2.0 + 3.0


def opaque_middle(value):
    prefix = value * 2.0
    print(prefix)
    return prefix + 1.0


def python_region_artifact(
    function,
    *,
    include_source_proof: bool = True,
) -> bytes:
    identities = capture_identities(function)
    fallback_identity = FallbackIdentity(
        function.__module__,
        function.__qualname__,
        identities.code.sha256,
    )
    compiled = compile_semantic(
        analyze_function(function, identities=identities)
    )
    if (
        compiled.reason_code != "verified_scalar_graph_break"
        or compiled.core_module is None
        or compiled.region_graph is None
    ):
        raise AssertionError("test function did not form a scalar graph break")
    module = compiled.core_module
    graph = compiled.region_graph
    if not include_source_proof:
        region = dataclasses.replace(
            module.python_regions[0],
            source_end=None,
        )
        module = rehash_semantic_module(
            dataclasses.replace(
                module,
                python_regions=(region,),
            )
        )
        graph = form_semantic_region_graph(module)
    return encode_artifact(
        build_artifact(
            module,
            graph,
            fallback_identity,
        )
    )


def encoded_artifact() -> bytes:
    captured = capture(CaptureRequest(affine))
    compiled = compile_semantic(captured)
    return encode_artifact(
        build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
    )


def raw_envelope(documents) -> bytes:
    body_parts = []
    for name, document in documents.items():
        name_bytes = name.encode("ascii")
        payload = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        body_parts.append(
            SECTION_HEADER.pack(
                1,
                len(name_bytes), len(payload), hashlib.sha256(payload).digest()
            )
            + name_bytes
            + payload
        )
    body = b"".join(body_parts)
    return ARTIFACT_HEADER.pack(
        ARTIFACT_MAGIC,
        1,
        0,
        ARTIFACT_HEADER.size + len(body),
        len(documents),
        hashlib.sha256(body).digest(),
    ) + body


class _LocalProviderFactory:
    """Interpreter-only unit seam; events must never label it as CinderX JIT."""

    def __init__(self):
        self.compile_count = 0
        self.continuation_payload_count = 0
        self.continuation_payload_values = []
        self.compiled_variants = []

    def compile(self, artifact, key, *, continuation=None):
        self.compile_count += 1
        registry = CapabilityRegistry(epoch=key.process.cluster_epoch)
        input_spec = artifact.input_access_specs[0]
        output_spec = artifact.output_access_spec
        input_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type=input_spec.scalar_type,
                nullable=input_spec.nullable,
            )
        )
        output_handle = registry.register(
            LocalScalarSlotBackend(
                scalar_type=output_spec.scalar_type,
                nullable=output_spec.nullable,
            )
        )
        compiled = compile_semantic_scalar_region(
            artifact.semantic_core_module,
            artifact.semantic_region_graph,
            input_spec=input_spec,
            output_spec=output_spec,
            registry=registry,
            execution_mode="python-interpreter-test-double",
            continuation_contract=(
                None if continuation is None else continuation.contract
            ),
        )
        if continuation is not None:
            def observed_payload(*arguments):
                self.continuation_payload_count += 1
                self.continuation_payload_values.append(arguments[7])
                return build_continuation_payload(*arguments)

            compiled.jit_function.__globals__[
                "_udf_build_continuation_payload"
            ] = observed_payload
        variant = ScalarProviderVariant(
            key,
            compiled,
            registry,
            input_handle,
            output_handle,
            ScalarExecutor(registry),
            (),
            continuation is not None,
        )
        self.compiled_variants.append(variant)
        return variant


class _FailingProviderFactory:
    def __init__(self):
        self.compile_count = 0

    def compile(self, _artifact, _key):
        self.compile_count += 1
        raise RuntimeError("controlled_compile_reject")


class _PostEntryVariant:
    execution_mode = "python-interpreter-test-double"
    intrinsic_load_count = 0
    code_hash = "f" * 64
    code_size = 1

    def execute(self, _value, *, boundary):
        boundary.commit()
        raise ArithmeticError("controlled-post-entry")


class _PostEntryFactory:
    def compile(self, _artifact, _key):
        return _PostEntryVariant()


class _DescriptorMissVariant(_PostEntryVariant):
    def execute(self, _value, *, boundary):
        raise PreSemanticsExecutionError("descriptor_epoch_mismatch")


class _DescriptorMissFactory:
    def compile(self, _artifact, _key):
        return _DescriptorMissVariant()


class _CommittedPreSemanticsVariant(_PostEntryVariant):
    def execute(self, _value, *, boundary):
        boundary.commit()
        raise PreSemanticsExecutionError("late_descriptor_miss")


class _CommittedPreSemanticsFactory:
    def compile(self, _artifact, _key):
        return _CommittedPreSemanticsVariant()


class WorkerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        @functools.wraps(affine)
        def daft_method(_self, value):
            self.calls.append(value)
            return affine(value)

        self.original = daft_method
        self.carrier = ProductionCarrierState.placeholder(
            "candidate-a", "a" * 64
        ).finalize(encoded_artifact())
        self.process = WorkerProcessKey(
            "epoch-a", "node-a", "worker-a", os.getpid(), "generation-a"
        )
        self.context = WorkerRuntimeContext("run-a", self.process, "partition-a", "attempt-a")
        self.target = RuntimeTarget(
            "3.14.3", "cpython-314-aarch64-linux-gnu", ("asimd",)
        )
        self.report = InMemoryRuntimeReport()

    def adapter(
        self,
        provider,
        *,
        artifact_loader=None,
        invocation_layout=None,
        target_provider=None,
    ):
        arguments = dict(
            candidate_id="candidate-a",
            original_callable=self.original,
            carrier=self.carrier,
            logical_schema="{'value': 'float64'}",
            context=self.context,
            target_provider=(
                (lambda: self.target)
                if target_provider is None
                else target_provider
            ),
            provider_factory=provider,
            event_sink=self.report,
        )
        if invocation_layout is not None:
            arguments["invocation_layout"] = invocation_layout
        if artifact_loader is not None:
            arguments["artifact_loader"] = artifact_loader
        return WorkerScalarAdapter(**arguments)

    def test_first_call_compiles_and_second_call_hits_same_process_variant(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)

        self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
        adapter.drain_compilation()
        self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

        self.assertEqual(provider.compile_count, 1)
        self.assertGreater(adapter._variants.budget_state()[1], 1)
        self.assertEqual(self.calls, [2.0])
        events = self.report.snapshot()
        self.assertEqual(
            [event.decision for event in events if event.stage == "provider"],
            ["compile", "hit"],
        )
        self.assertFalse(any(event.stage == "jit" for event in events))
        self.assertEqual(
            sum(event.decision == "semantic_execute" for event in events), 1
        )
        self.assertEqual(len({event.variant_key for event in events if event.variant_key}), 1)

    def test_diagnostics_off_keeps_the_existing_hot_event_sink(self):
        adapter = self.adapter(_LocalProviderFactory())

        self.assertIs(adapter._event_sink, self.report)
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        with mock.patch.object(
            adapter,
            "_finalize_diagnostic_runtime",
            side_effect=AssertionError("diagnostics off hot path polluted"),
        ):
            self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

    def _diagnostic_adapter(self, root: Path, *, perf: bool = False):
        profile = "full" if perf else "summary"
        policy = resolve_diagnostic_policy(
            {
                "UDFJIT_DIAGNOSTICS": profile,
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "record" if perf else "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            },
            DiagnosticRuntimeContext(
                dedicated_worker=perf,
                workspace_root=root / "workspace",
                home_root=root / "home",
            ),
        )
        payload = encoded_artifact()
        carrier = ProductionCarrierState.placeholder(
            "candidate-a",
            "a" * 64,
            diagnostic_policy=policy,
        ).finalize(payload)
        context = WorkerRuntimeContext(
            "run-a",
            self.process,
            "partition-a",
            "attempt-a",
            diagnostic_policy=policy,
            diagnostic_bootstrapped=perf,
        )
        return WorkerScalarAdapter(
            candidate_id="candidate-a",
            original_callable=self.original,
            carrier=carrier,
            logical_schema="{'value': 'float64'}",
            context=context,
            target_provider=lambda: self.target,
            provider_factory=_LocalProviderFactory(),
            event_sink=self.report,
        )

    def test_first_successful_hot_hit_publishes_diagnostic_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = self._diagnostic_adapter(root)

            self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
            adapter.drain_compilation()
            self.assertEqual(tuple((root / "diagnostics").glob("diagnostic-*")), ())

            runtime = adapter._diagnostic_runtime
            self.assertIsNotNone(runtime)
            with mock.patch.object(
                runtime,
                "finalize",
                wraps=runtime.finalize,
            ) as finalize:
                self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)
                self.assertEqual(adapter.invoke((None, 5.0), {}), 13.0)
                finalize.assert_called_once_with()

            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            self.assertTrue((bundles[0] / "COMPLETE").is_file())
            adapter.close()
            self.assertEqual(
                len(tuple((root / "diagnostics").glob("diagnostic-*"))),
                1,
            )

    def test_perf_record_waits_for_evidence_until_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = self._diagnostic_adapter(root, perf=True)

            self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
            adapter.drain_compilation()
            self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

            self.assertEqual(tuple((root / "diagnostics").glob("diagnostic-*")), ())
            adapter.close()
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            manifest = json.loads((bundles[0] / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "partial")

    def test_hot_hit_finalize_failure_preserves_result_and_close_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = self._diagnostic_adapter(root)
            runtime = adapter._diagnostic_runtime
            self.assertIsNotNone(runtime)
            real_finalize = runtime.finalize
            finalize_calls = 0

            def flaky_finalize():
                nonlocal finalize_calls
                finalize_calls += 1
                if finalize_calls == 1:
                    raise RuntimeError("controlled_finalize_failure")
                return real_finalize()

            with mock.patch.object(
                runtime,
                "finalize",
                side_effect=flaky_finalize,
            ):
                self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
                adapter.drain_compilation()
                self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)
                self.assertEqual(
                    tuple((root / "diagnostics").glob("diagnostic-*")),
                    (),
                )
                adapter.close()

            self.assertEqual(finalize_calls, 2)
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            manifest = json.loads((bundles[0] / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "partial")
            adapter.close()
            self.assertEqual(finalize_calls, 2)

    def test_swallowed_bundle_failure_remains_retryable_at_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = self._diagnostic_adapter(root)
            runtime = adapter._diagnostic_runtime
            self.assertIsNotNone(runtime)
            writer = runtime._session._bundle_writer
            self.assertIsNotNone(writer)
            real_complete = writer.complete
            complete_calls = 0

            def flaky_complete(status):
                nonlocal complete_calls
                complete_calls += 1
                if complete_calls == 1:
                    raise OSError("controlled_bundle_failure")
                return real_complete(status)

            with mock.patch.object(
                writer,
                "complete",
                side_effect=flaky_complete,
            ):
                self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
                adapter.drain_compilation()
                self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)
                self.assertEqual(
                    tuple((root / "diagnostics").glob("diagnostic-*")),
                    (),
                )
                adapter.close()

            self.assertEqual(complete_calls, 2)
            bundles = tuple((root / "diagnostics").glob("diagnostic-*"))
            self.assertEqual(len(bundles), 1)
            manifest = json.loads((bundles[0] / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "partial")

    def test_warm_hit_reuses_process_stable_guard_inputs(self):
        target_provider = mock.Mock(return_value=self.target)
        adapter = self.adapter(
            _LocalProviderFactory(),
            target_provider=target_provider,
        )

        with mock.patch.object(
            worker_runtime,
            "capture_identities",
            wraps=worker_runtime.capture_identities,
        ) as capture_spy:
            with mock.patch.object(
                worker_runtime,
                "_sha256_text",
                wraps=worker_runtime._sha256_text,
            ) as sha_spy:
                adapter.invoke((None, 2.0), {})
                adapter.drain_compilation()
                counts_after_initialization = (
                    capture_spy.call_count,
                    target_provider.call_count,
                    sha_spy.call_count,
                )

                self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

        self.assertEqual(
            (
                capture_spy.call_count,
                target_provider.call_count,
                sha_spy.call_count,
            ),
            counts_after_initialization,
        )

    def test_warm_hit_acquires_without_re_resolving_the_variant(self):
        adapter = self.adapter(_LocalProviderFactory())
        self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
        adapter.drain_compilation()
        self.assertEqual(adapter.invoke((None, 3.0), {}), 9.0)

        with mock.patch.object(
            adapter._variants,
            "resolve",
            wraps=adapter._variants.resolve,
        ) as resolve, mock.patch.object(
            adapter._variants,
            "acquire",
            wraps=adapter._variants.acquire,
        ) as acquire:
            self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

        resolve.assert_not_called()
        acquire.assert_called_once_with(adapter._key)
        adapter.close()

    def test_guard_overrides_keep_the_complete_slow_guard(self):
        target_provider = mock.Mock(return_value=self.target)
        adapter = self.adapter(
            _LocalProviderFactory(),
            target_provider=target_provider,
        )
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()

        with mock.patch.object(
            worker_runtime,
            "capture_identities",
            wraps=worker_runtime.capture_identities,
        ) as capture_spy:
            with mock.patch.object(
                worker_runtime,
                "_sha256_text",
                wraps=worker_runtime._sha256_text,
            ) as sha_spy:
                self.assertEqual(
                    adapter.invoke(
                        (None, 4.0),
                        {},
                        guard_overrides=WorkerGuardOverrides(),
                    ),
                    11.0,
                )

        self.assertGreater(capture_spy.call_count, 0)
        self.assertGreater(target_provider.call_count, 1)
        self.assertGreater(sha_spy.call_count, 0)

    def test_replacing_wrapped_user_function_falls_back_before_provider_entry(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()
        event_count = len(self.report.snapshot())

        def replacement(value):
            return value + 100.0

        wrapped = vars(self.original)["__wrapped__"]
        vars(self.original)["__wrapped__"] = replacement
        try:
            result = adapter.invoke((None, 3.0), {})
        finally:
            vars(self.original)["__wrapped__"] = wrapped

        self.assertEqual(result, 9.0)
        self.assertEqual(self.calls, [3.0])
        self.assertEqual(provider.compile_count, 1)
        self.assertTrue(
            any(
                event.decision == "fallback"
                and event.reason_code == "callable_mismatch"
                for event in self.report.snapshot()[event_count:]
            )
        )

    def test_replacing_user_code_falls_back_before_provider_entry(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()
        event_count = len(self.report.snapshot())

        def replacement(value):
            return value + 100.0

        original_code = affine.__code__
        affine.__code__ = replacement.__code__
        try:
            result = adapter.invoke((None, 3.0), {})
        finally:
            affine.__code__ = original_code

        self.assertEqual(result, 103.0)
        self.assertEqual(self.calls, [3.0])
        self.assertEqual(provider.compile_count, 1)
        self.assertTrue(
            any(
                event.decision == "fallback"
                and event.reason_code == "callable_mismatch"
                for event in self.report.snapshot()[event_count:]
            )
        )

    def test_replacing_original_callable_code_falls_back_before_provider_entry(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()
        event_count = len(self.report.snapshot())

        def replacement_factory(holder):
            def replacement(_self, value):
                holder.calls.append(value)
                return value + 100.0

            return replacement

        replacement = replacement_factory(self)
        original_code = self.original.__code__
        self.original.__code__ = replacement.__code__
        try:
            result = adapter.invoke((None, 3.0), {})
        finally:
            self.original.__code__ = original_code

        self.assertEqual(result, 103.0)
        self.assertEqual(self.calls, [3.0])
        self.assertEqual(provider.compile_count, 1)
        self.assertTrue(
            any(
                event.decision == "fallback"
                and event.reason_code == "callable_mismatch"
                for event in self.report.snapshot()[event_count:]
            )
        )

    def test_supported_hot_path_uses_provider_slot_without_second_physicalizer(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()

        with mock.patch.object(
            ScalarPhysicalizer,
            "open_call",
            side_effect=AssertionError("duplicate scalar physicalization"),
        ):
            result = adapter.invoke((None, 3.0), {})

        self.assertEqual(result, 9.0)
        self.assertEqual(self.calls, [])
        self.assertTrue(
            any(
                event.decision == "semantic_execute"
                for event in self.report.snapshot()
            )
        )

        variant = provider.compiled_variants[0]
        with mock.patch.object(
            ScalarPhysicalizer,
            "close",
            side_effect=AssertionError("duplicate scalar physicalizer close"),
        ):
            adapter.close()

        for handle in (variant.input_handle, variant.output_handle):
            with self.assertRaises(CapabilityError) as raised:
                variant.registry.descriptor(handle)
            self.assertIs(raised.exception.code, CapabilityRejectCode.UNKNOWN_ACCESS)

    def test_from_environment_refreshes_ray_task_id_when_partition_is_unset(self):
        runtime = SimpleNamespace(
            get_task_id=mock.Mock(side_effect=("task-build", "task-live"))
        )
        fake_ray = SimpleNamespace(
            get_runtime_context=mock.Mock(return_value=runtime)
        )
        environment = {
            "UDFJIT_CLUSTER_EPOCH": "epoch-a",
            "UDFJIT_RUN_ID": "run-a",
            "UDFJIT_NODE_ID": "node-a",
            "UDFJIT_ACTOR_WORKER_ID": "worker-a",
        }

        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.dict(sys.modules, {"ray": fake_ray}):
                context = WorkerRuntimeContext.from_environment()
                attribution = context.event_attribution()

        self.assertEqual(context.partition_id, "task-build")
        self.assertEqual(context.task_attempt, "")
        self.assertTrue(context.refresh_partition_from_ray)
        self.assertEqual(attribution, ("task-live", ""))
        self.assertEqual(fake_ray.get_runtime_context.call_count, 2)
        self.assertEqual(runtime.get_task_id.call_count, 2)

    def test_event_attribution_uses_the_process_local_ray_task_id(self):
        context = WorkerRuntimeContext(
            "run-a",
            self.process,
            "task-build",
            "",
            refresh_partition_from_ray=True,
        )
        task_id = SimpleNamespace(hex=mock.Mock(return_value="task-live"))
        worker_module = ModuleType("ray._private.worker")
        worker_module.global_worker = SimpleNamespace(current_task_id=task_id)
        fake_ray = SimpleNamespace(
            get_runtime_context=mock.Mock(
                side_effect=AssertionError("public Ray context path is slower")
            )
        )

        with mock.patch.dict(
            sys.modules,
            {
                "ray": fake_ray,
                "ray._private.worker": worker_module,
            },
        ):
            attribution = context.event_attribution()

        self.assertEqual(attribution, ("task-live", ""))
        task_id.hex.assert_called_once_with()
        fake_ray.get_runtime_context.assert_not_called()

    def test_from_environment_freezes_diagnostics_once_at_worker_start(self):
        environment = {
            "UDFJIT_CLUSTER_EPOCH": "epoch-a",
            "UDFJIT_RUN_ID": "run-a",
            "UDFJIT_NODE_ID": "node-a",
            "UDFJIT_ACTOR_WORKER_ID": "worker-a",
            "UDFJIT_PARTITION_ID": "partition-a",
            "UDFJIT_DIAGNOSTICS": "off",
        }

        with mock.patch.dict(os.environ, environment, clear=True):
            context = WorkerRuntimeContext.from_environment()

        self.assertIs(
            context.diagnostic_policy,
            OFF_DIAGNOSTIC_POLICY,
        )

    def test_worker_rejects_carrier_diagnostic_policy_drift(self):
        with self.assertRaisesRegex(
            ValueError,
            "worker_diagnostic_policy_hash_mismatch",
        ):
            WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=self.original,
                carrier=dataclasses.replace(
                    self.carrier,
                    diagnostic_policy_sha256="b" * 64,
                ),
                logical_schema="{'value': 'float64'}",
                context=self.context,
            )

    def test_worker_rejects_invocation_layout_epoch_mismatch(self):
        layout = InvocationLayoutContract.for_types(
            ("float64",),
            "float64",
            epoch="other-epoch",
        )

        with self.assertRaisesRegex(
            ValueError,
            "worker_invocation_layout_mismatch",
        ):
            self.adapter(
                _LocalProviderFactory(),
                invocation_layout=layout,
            )

    def test_artifact_type_mismatch_falls_back_before_provider(self):
        layout = InvocationLayoutContract.for_types(
            ("float32",),
            "float64",
            epoch="epoch-a",
        )
        provider = _LocalProviderFactory()

        result = self.adapter(
            provider,
            invocation_layout=layout,
        ).invoke((None, 2.0), {})

        self.assertEqual(result, 7.0)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(provider.compile_count, 0)
        self.assertTrue(
            any(
                event.reason_code == "schema_mismatch"
                for event in self.report.snapshot()
            )
        )

    def test_policy_hash_and_job_namespace_bind_worker_managers(self):
        drifted = PolicySnapshot.mainline(
            version="drifted",
            budgets={
                "code_bytes": 1024 * 1024,
                "compile_concurrency": 1,
                "variant_limit": 2,
            },
        )
        with self.assertRaisesRegex(
            ValueError,
            "worker_policy_hash_mismatch",
        ):
            WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=self.original,
                carrier=self.carrier,
                logical_schema="{'value': 'float64'}",
                context=dataclasses.replace(
                    self.context,
                    policy=drifted,
                ),
            )

        first = WorkerScalarAdapter(
            candidate_id="candidate-a",
            original_callable=self.original,
            carrier=self.carrier,
            logical_schema="{'value': 'float64'}",
            context=dataclasses.replace(self.context, run_id="job-one"),
        )
        second = WorkerScalarAdapter(
            candidate_id="candidate-a",
            original_callable=self.original,
            carrier=self.carrier,
            logical_schema="{'value': 'float64'}",
            context=dataclasses.replace(self.context, run_id="job-two"),
        )
        try:
            self.assertIsNot(first._variants, second._variants)
            self.assertEqual(
                first._variants.budget_limits(),
                (
                    self.carrier.policy.budgets["variant_limit"],
                    self.carrier.policy.budgets["code_bytes"],
                ),
            )
        finally:
            first.close()
            second.close()

    def test_shared_manager_can_evict_between_active_adapters(self):
        policy = PolicySnapshot.mainline(
            version="single-variant-test",
            budgets={
                "code_bytes": 1024 * 1024,
                "compile_concurrency": 1,
                "variant_limit": 1,
            },
        )
        carrier = ProductionCarrierState.placeholder(
            "candidate-a",
            "a" * 64,
            policy=policy,
        ).finalize(encoded_artifact())
        context = dataclasses.replace(
            self.context,
            run_id="single-variant-job",
            policy=policy,
        )
        first_provider = _LocalProviderFactory()
        second_provider = _LocalProviderFactory()

        with mock.patch.object(
            worker_runtime,
            "CinderXScalarProviderFactory",
            _LocalProviderFactory,
        ):
            first = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=self.original,
                carrier=carrier,
                logical_schema="{'value': 'float64'}",
                context=context,
                target_provider=lambda: self.target,
                provider_factory=first_provider,
                event_sink=self.report,
            )
            second = WorkerScalarAdapter(
                candidate_id="candidate-a",
                original_callable=self.original,
                carrier=carrier,
                logical_schema="{'other': 'float64'}",
                context=context,
                target_provider=lambda: self.target,
                provider_factory=second_provider,
                event_sink=self.report,
            )
            try:
                self.assertEqual(first.invoke((None, 2.0), {}), 7.0)
                first.drain_compilation()
                self.assertEqual(first.invoke((None, 3.0), {}), 9.0)

                self.assertEqual(second.invoke((None, 4.0), {}), 11.0)
                second.drain_compilation()
                self.assertEqual(second.invoke((None, 5.0), {}), 13.0)

                self.assertEqual(first_provider.compile_count, 1)
                self.assertEqual(second_provider.compile_count, 1)
                self.assertEqual(first._variants.budget_state()[0], 1)
            finally:
                first.close()
                second.close()

    def test_each_invoke_freezes_one_ray_task_identity_for_all_events(self):
        provider = _LocalProviderFactory()
        self.context = WorkerRuntimeContext(
            "run-a",
            self.process,
            "partition-build",
            "",
            refresh_partition_from_ray=True,
        )
        adapter = self.adapter(provider)
        runtime = SimpleNamespace(
            get_task_id=mock.Mock(side_effect=("task-one", "task-two"))
        )
        fake_ray = SimpleNamespace(
            get_runtime_context=mock.Mock(return_value=runtime)
        )

        with mock.patch.dict(sys.modules, {"ray": fake_ray}):
            self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
            first_count = len(self.report.snapshot())
            self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

        events = self.report.snapshot()
        self.assertEqual(fake_ray.get_runtime_context.call_count, 2)
        self.assertEqual(runtime.get_task_id.call_count, 2)
        self.assertTrue(events[:first_count])
        self.assertTrue(events[first_count:])
        self.assertEqual(
            {event.partition_id for event in events[:first_count]},
            {"task-one"},
        )
        self.assertEqual(
            {event.partition_id for event in events[first_count:]},
            {"task-two"},
        )

    def test_emit_uses_frozen_attribution_without_ray_context_lookup(self):
        adapter = self.adapter(_LocalProviderFactory())
        fake_ray = SimpleNamespace(
            get_runtime_context=mock.Mock(
                side_effect=AssertionError("event emission must not re-enter Ray")
            )
        )

        with mock.patch.dict(sys.modules, {"ray": fake_ray}):
            adapter._emit("execute", "probe", "success")

        fake_ray.get_runtime_context.assert_not_called()
        event = self.report.snapshot()[-1]
        self.assertEqual(event.partition_id, "partition-a")
        self.assertEqual(event.task_attempt, "attempt-a")

    def test_outer_guard_miss_falls_back_once_without_compile_or_semantic_execute(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)

        result = adapter.invoke(
            (None, 2.0),
            {},
            guard_overrides=WorkerGuardOverrides(logical_schema="{'value': 'int64'}"),
        )

        self.assertEqual(result, 7.0)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(provider.compile_count, 0)
        events = self.report.snapshot()
        self.assertTrue(any(event.reason_code == "schema_mismatch" for event in events))
        self.assertFalse(any(event.decision == "semantic_execute" for event in events))

    def test_each_portable_or_target_change_misses_before_provider_entry(self):
        cases = (
            WorkerGuardOverrides(artifact_content_sha256="b" * 64),
            WorkerGuardOverrides(experiment_manifest_sha256="b" * 64),
            WorkerGuardOverrides(semantic_hash="b" * 64),
            WorkerGuardOverrides(callable_code_sha256="b" * 64),
            WorkerGuardOverrides(target_python="3.14.4"),
            WorkerGuardOverrides(
                target_soabi="cpython-314-x86_64-linux-gnu"
            ),
            WorkerGuardOverrides(cpu_features=("sve",)),
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.calls.clear()
                self.report = InMemoryRuntimeReport()
                provider = _LocalProviderFactory()

                result = self.adapter(provider).invoke(
                    (None, 2.0), {}, guard_overrides=overrides
                )

                self.assertEqual(result, 7.0)
                self.assertEqual(self.calls, [2.0])
                self.assertEqual(provider.compile_count, 0)
                self.assertFalse(
                    any(
                        event.decision == "semantic_execute"
                        for event in self.report.snapshot()
                    )
                )

    def test_compile_failure_is_pre_semantics_and_calls_original_once(self):
        provider = _FailingProviderFactory()
        adapter = self.adapter(provider)
        first = adapter.invoke((None, 3.0), {})
        adapter.drain_compilation()
        self.calls.clear()
        second = adapter.invoke((None, 3.0), {})

        self.assertEqual((first, second), (9.0, 9.0))
        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(self.calls, [3.0])
        self.assertTrue(
            any(
                event.reason_code == "negative_cache"
                for event in self.report.snapshot()
            )
        )
        self.assertFalse(
            any(event.decision == "semantic_execute" for event in self.report.snapshot())
        )

    def test_descriptor_preflight_miss_falls_back_once_before_semantic_execute(self):
        adapter = self.adapter(_DescriptorMissFactory())
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()
        result = adapter.invoke((None, 3.0), {})

        self.assertEqual(result, 9.0)
        self.assertEqual(self.calls, [3.0])
        events = self.report.snapshot()
        self.assertTrue(
            any(event.reason_code == "descriptor_epoch_mismatch" for event in events)
        )
        self.assertFalse(any(event.decision == "semantic_execute" for event in events))

    def test_wrong_python_type_falls_back_without_allocating_a_variant(self):
        provider = _LocalProviderFactory()

        result = self.adapter(provider).invoke((None, 3), {})

        self.assertEqual(result, 9.0)
        self.assertEqual(self.calls, [3])
        self.assertEqual(provider.compile_count, 0)

    def test_post_entry_failure_propagates_without_replay(self):
        adapter = self.adapter(_PostEntryFactory())
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()

        with self.assertRaisesRegex(ArithmeticError, "controlled-post-entry"):
            adapter.invoke((None, 3.0), {})

        self.assertEqual(self.calls, [])
        self.assertTrue(
            any(
                event.decision == "post_entry_failure"
                for event in self.report.snapshot()
            )
        )

    def test_committed_pre_semantics_error_propagates_without_replay(self):
        adapter = self.adapter(_CommittedPreSemanticsFactory())
        adapter.invoke((None, 2.0), {})
        adapter.drain_compilation()
        self.calls.clear()

        with self.assertRaisesRegex(
            PreSemanticsExecutionError,
            "late_descriptor_miss",
        ):
            adapter.invoke((None, 3.0), {})

        self.assertEqual(self.calls, [])
        self.assertTrue(
            any(
                event.decision == "post_entry_failure"
                and event.reason_code == "late_descriptor_miss"
                for event in self.report.snapshot()
            )
        )

    def test_verified_python_region_uses_production_continuation_once(self):
        @functools.wraps(opaque_middle)
        def daft_method(_self, value):
            self.calls.append(value)
            return opaque_middle(value)

        self.original = daft_method
        self.carrier = ProductionCarrierState.placeholder(
            "candidate-a", "a" * 64
        ).finalize(python_region_artifact(opaque_middle))
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)

        with mock.patch("builtins.print") as region_call:
            with mock.patch(
                (
                    "python_udf_jit.integration.daft_ray.worker."
                    "reference_resume_semantic"
                ),
                wraps=reference_resume_semantic,
            ) as suffix_call:
                adapter.invoke((None, 2.0), {})
                adapter.drain_compilation()
                self.calls.clear()
                region_call.reset_mock()
                result = adapter.invoke((None, 3.0), {})

        self.assertEqual(result, 7.0)
        region_call.assert_called_once_with(6.0)
        suffix_call.assert_called_once()
        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(provider.continuation_payload_count, 1)
        self.assertEqual(provider.continuation_payload_values, [(6.0,)])
        self.assertEqual(self.calls, [])
        self.assertTrue(
            any(
                event.decision == "semantic_execute"
                for event in self.report.snapshot()
            )
        )

    def test_unproven_python_region_falls_back_before_provider_entry(self):
        @functools.wraps(opaque_middle)
        def daft_method(_self, value):
            self.calls.append(value)
            return opaque_middle(value)

        self.original = daft_method
        self.carrier = ProductionCarrierState.placeholder(
            "candidate-a", "a" * 64
        ).finalize(
            python_region_artifact(
                opaque_middle,
                include_source_proof=False,
            )
        )
        provider = _LocalProviderFactory()

        with mock.patch("builtins.print") as region_call:
            result = self.adapter(provider).invoke((None, 3.0), {})

        self.assertEqual(result, 7.0)
        region_call.assert_called_once_with(6.0)
        self.assertEqual(self.calls, [3.0])
        self.assertEqual(provider.compile_count, 0)
        self.assertTrue(
            any(
                event.decision == "fallback"
                and event.reason_code == "continuation_proof_incomplete"
                for event in self.report.snapshot()
            )
        )

    def test_fallback_exception_is_not_retried(self):
        calls = []

        @functools.wraps(affine)
        def failing_original(_self, value):
            calls.append(value)
            raise LookupError("original-failure")

        self.original = failing_original
        adapter = self.adapter(_LocalProviderFactory())

        with self.assertRaisesRegex(LookupError, "original-failure"):
            adapter.invoke(
                (None, 2.0),
                {},
                guard_overrides=WorkerGuardOverrides(
                    logical_schema="{'value': 'int64'}"
                ),
            )

        self.assertEqual(calls, [2.0])

    def test_hash_self_consistent_illegal_opcode_is_rejected_before_provider(self):
        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        built = build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
        operations = list(compiled.core_module.operations)
        binary_index = next(
            index
            for index, operation in enumerate(operations)
            if operation.op == "binary.mul"
        )
        operations[binary_index] = dataclasses.replace(
            operations[binary_index],
            op="binary.div",
        )
        invalid_module = rehash_semantic_module(
            dataclasses.replace(
                compiled.core_module,
                operations=tuple(operations),
            )
        )
        valid = built.section_documents()
        valid["semantic_core_ir"] = invalid_module.to_document()
        valid["guard"]["semantic_core_hash"] = (
            invalid_module.semantic_hash
        )
        invalid = raw_envelope(valid)
        self.carrier = ProductionCarrierState.placeholder(
            "candidate-a", "a" * 64
        ).finalize(invalid)
        provider = _LocalProviderFactory()

        result = self.adapter(provider).invoke((None, 2.0), {})

        self.assertEqual(result, 7.0)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(provider.compile_count, 0)
        self.assertFalse(
            any(event.decision == "semantic_execute" for event in self.report.snapshot())
        )

    def test_carrier_claim_must_match_actual_artifact_payload(self):
        payload = encoded_artifact()
        self.carrier = ProductionCarrierState(
            schema_version=1,
            candidate_id="candidate-a",
            manifest_sha256="a" * 64,
            handle=InlineArtifactHandle(
                "inline-artifact",
                "b" * 64,
                len(payload),
                payload,
            ),
            policy=self.context.policy,
        )
        provider = _LocalProviderFactory()

        result = self.adapter(provider).invoke((None, 2.0), {})

        self.assertEqual(result, 7.0)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(provider.compile_count, 0)
        self.assertTrue(
            any(
                event.reason_code == "artifact_mismatch"
                for event in self.report.snapshot()
            )
        )

    def test_dependency_rejection_falls_back_once_before_provider_entry(self):
        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        manifest = dataclasses.replace(
            DEFAULT_MANIFEST,
            dependency_requirements=(
                DependencyRequirement(
                    "python-udf-jit-definitely-missing",
                    "1.0.0",
                ),
            ),
        )
        self.carrier = ProductionCarrierState.placeholder(
            "candidate-a",
            "a" * 64,
        ).finalize(
            encode_artifact(
                build_artifact(
                    compiled.core_module,
                    compiled.region_graph,
                    captured.fallback_identity,
                    manifest,
                )
            )
        )
        loader = ArtifactLoader(
            dependency_resolver=lambda _distribution: None,
        )
        provider = _LocalProviderFactory()

        result = self.adapter(
            provider,
            artifact_loader=loader,
        ).invoke((None, 2.0), {})

        self.assertEqual(result, 7.0)
        self.assertEqual(self.calls, [2.0])
        self.assertEqual(provider.compile_count, 0)
        self.assertTrue(
            any(
                event.reason_code == "artifact_load_rejected"
                for event in self.report.snapshot()
            )
        )
        self.assertNotIn(
            "python-udf-jit-definitely-missing",
            repr(self.report.snapshot()),
        )


if __name__ == "__main__":
    unittest.main()
