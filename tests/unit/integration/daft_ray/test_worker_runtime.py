from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.diagnostics.report import InMemoryRuntimeReport
from python_udf_jit.integration.daft_ray.carrier import (
    InlineArtifactHandle,
    ProductionCarrierState,
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
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.compiler import compile_scalar_region
from python_udf_jit.provider.scalar_python.executor import (
    PreSemanticsExecutionError,
    ScalarExecutor,
    ScalarProviderVariant,
)
from python_udf_jit.runtime.layout import LocalScalarSlotBackend
from python_udf_jit.runtime.variant import WorkerProcessKey


def affine(value):
    return value * 2.0 + 3.0


def encoded_artifact() -> bytes:
    module = lower_capture(capture(CaptureRequest(affine)))
    return encode_artifact(
        build_artifact(module, form_verified_region(module), module.fallback_identity)
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

    def compile(self, artifact, key):
        self.compile_count += 1
        registry = CapabilityRegistry(epoch=key.process.cluster_epoch)
        handle = registry.register(LocalScalarSlotBackend())
        compiled = compile_scalar_region(
            artifact.core_module,
            artifact.region,
            registry=registry,
            execution_mode="python-interpreter-test-double",
        )
        return ScalarProviderVariant(
            key, compiled, registry, handle, ScalarExecutor(registry), 0
        )


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

    def execute(self, _value):
        raise ArithmeticError("controlled-post-entry")


class _PostEntryFactory:
    def compile(self, _artifact, _key):
        return _PostEntryVariant()


class _DescriptorMissVariant(_PostEntryVariant):
    def execute(self, _value):
        raise PreSemanticsExecutionError("descriptor_epoch_mismatch")


class _DescriptorMissFactory:
    def compile(self, _artifact, _key):
        return _DescriptorMissVariant()


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

    def adapter(self, provider):
        return WorkerScalarAdapter(
            candidate_id="candidate-a",
            original_callable=self.original,
            carrier=self.carrier,
            logical_schema="{'value': 'float64'}",
            context=self.context,
            target_provider=lambda: self.target,
            provider_factory=provider,
            event_sink=self.report,
        )

    def test_first_call_compiles_and_second_call_hits_same_process_variant(self):
        provider = _LocalProviderFactory()
        adapter = self.adapter(provider)

        self.assertEqual(adapter.invoke((None, 2.0), {}), 7.0)
        self.assertEqual(adapter.invoke((None, 4.0), {}), 11.0)

        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(self.calls, [])
        events = self.report.snapshot()
        self.assertEqual(
            [event.decision for event in events if event.stage == "provider"],
            ["compile", "hit"],
        )
        self.assertFalse(any(event.stage == "jit" for event in events))
        self.assertEqual(
            sum(event.decision == "semantic_execute" for event in events), 2
        )
        self.assertEqual(len({event.variant_key for event in events if event.variant_key}), 1)

    def test_from_environment_captures_ray_task_id_once_when_partition_is_unset(self):
        runtime = SimpleNamespace(get_task_id=mock.Mock(return_value="task-a"))
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

        self.assertEqual(context.partition_id, "task-a")
        self.assertEqual(context.task_attempt, "")
        fake_ray.get_runtime_context.assert_called_once_with()
        runtime.get_task_id.assert_called_once_with()

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
        result = self.adapter(provider).invoke((None, 3.0), {})

        self.assertEqual(result, 9.0)
        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(self.calls, [3.0])
        self.assertFalse(
            any(event.decision == "semantic_execute" for event in self.report.snapshot())
        )

    def test_descriptor_preflight_miss_falls_back_once_before_semantic_execute(self):
        result = self.adapter(_DescriptorMissFactory()).invoke((None, 3.0), {})

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

        with self.assertRaisesRegex(ArithmeticError, "controlled-post-entry"):
            adapter.invoke((None, 3.0), {})

        self.assertEqual(self.calls, [])
        self.assertTrue(
            any(
                event.decision == "post_entry_failure"
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
        valid = build_artifact(
            lower_capture(capture(CaptureRequest(affine))),
            form_verified_region(lower_capture(capture(CaptureRequest(affine)))),
            capture(CaptureRequest(affine)).fallback_identity,
        ).section_documents()
        valid["core_ir"]["nodes"][2]["op"] = "div.f64"
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


if __name__ == "__main__":
    unittest.main()
