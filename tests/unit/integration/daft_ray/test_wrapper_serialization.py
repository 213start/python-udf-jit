from __future__ import annotations

import operator
import os
import pickle
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.integration.daft_ray.carrier import (
    DEFAULT_INLINE_ARTIFACT_THRESHOLD,
    ObjectRefArtifactHandle,
    ProductionCarrierState,
)
from python_udf_jit.integration.daft_ray.objectref_bridge import (
    clear_driver_artifact_references,
    driver_artifact_references,
)
from python_udf_jit.integration.daft_ray.invocation_layout import (
    InvocationLayoutContract,
)
from python_udf_jit.integration.daft_ray.wrapper import (
    WRAPPER_SERIALIZATION_VERSION,
    FallbackOnlyWrapper,
)


class OriginalFailure(RuntimeError):
    pass


def fail_original(value):
    raise OriginalFailure(f"original:{value}")


class CountingCallable:
    def __init__(self):
        self.calls = 0

    def __call__(self, value):
        self.calls += 1
        return value * 2


class _Adapter:
    owner_pid = os.getpid()

    def __init__(self, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = 0

    def invoke(self, _args, _kwargs):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class _TypedAdapter:
    owner_pid = os.getpid()

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def invoke(self, _args, _kwargs):
        self.calls += 1
        return self.outcome


def make_wrapper(original=operator.add):
    return FallbackOnlyWrapper(
        candidate_id="candidate-test",
        original_callable=original,
        carrier=ProductionCarrierState.placeholder("candidate-test", "b" * 64),
    )


def exact_unicode_layout() -> InvocationLayoutContract:
    return InvocationLayoutContract.for_types(
        ("string",),
        "string",
        epoch="layout-epoch",
    )


class WrapperSerializationTest(unittest.TestCase):
    def setUp(self):
        clear_driver_artifact_references()

    def tearDown(self):
        clear_driver_artifact_references()

    def test_pickle_roundtrip_preserves_candidate_carrier_and_fallback(self):
        wrapper = make_wrapper()
        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertEqual(restored.candidate_id, wrapper.candidate_id)
        self.assertEqual(restored.carrier.state_sha256, wrapper.carrier.state_sha256)
        self.assertEqual(restored(19, 23), 42)

    def test_pickle_roundtrip_drops_process_local_worker_adapter(self):
        wrapper = make_wrapper()
        adapter = _Adapter(result=42)
        wrapper._worker_adapter = adapter

        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertIs(wrapper._worker_adapter, adapter)
        self.assertIsNone(restored._worker_adapter)

    def test_pickle_roundtrip_drops_process_local_typed_loop_adapter(self):
        wrapper = make_wrapper()
        adapter = object()
        wrapper._typed_loop_adapter = adapter
        wrapper._typed_loop_terminal_bypass = True

        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertIs(wrapper._typed_loop_adapter, adapter)
        self.assertIsNone(restored._typed_loop_adapter)
        self.assertFalse(restored._typed_loop_terminal_bypass)

    def test_serialized_state_declares_the_current_version(self):
        state = make_wrapper().__getstate__()

        self.assertEqual(
            state["_serialization_version"],
            WRAPPER_SERIALIZATION_VERSION,
        )

    def test_deserialization_rejects_missing_or_unsupported_versions(self):
        state = make_wrapper().__getstate__()

        for version in (
            None,
            True,
            0,
            WRAPPER_SERIALIZATION_VERSION + 1,
        ):
            rejected = dict(state)
            if version is None:
                rejected.pop("_serialization_version")
            else:
                rejected["_serialization_version"] = version
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ValueError,
                    "wrapper_serialization_version_unsupported",
                ):
                    make_wrapper().__setstate__(rejected)

    def test_internal_event_failure_still_calls_original_once(self):
        original = CountingCallable()
        wrapper = make_wrapper(original)

        with mock.patch(
            "python_udf_jit.integration.daft_ray.wrapper.events.try_emit",
            side_effect=RuntimeError("event sink unavailable"),
        ):
            result = wrapper(21)

        self.assertEqual(result, 42)
        self.assertEqual(original.calls, 1)

    def test_original_exception_type_and_message_are_preserved(self):
        wrapper = make_wrapper(fail_original)

        with self.assertRaisesRegex(OriginalFailure, "original:7"):
            wrapper(7)

    def test_finalize_is_first_write_and_pickle_stable(self):
        wrapper = make_wrapper()

        self.assertTrue(wrapper.finalize("{'x': 'float64'}", "projection"))
        self.assertFalse(wrapper.finalize("{'x': 'float64'}", "projection"))
        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertEqual(restored.logical_schema, "{'x': 'float64'}")
        self.assertEqual(restored.usage_context, "projection")
        self.assertEqual(restored(20, 22), 42)

    def test_large_artifact_uses_ray_object_ref_when_driver_is_initialized(self):
        wrapper = make_wrapper()
        reference = ("ray-object-ref", "opaque")
        fake_ray = SimpleNamespace(
            is_initialized=mock.Mock(return_value=True),
            put=mock.Mock(return_value=reference),
        )

        with mock.patch.dict(sys.modules, {"ray": fake_ray}):
            self.assertTrue(
                wrapper.finalize(
                    "{'x': 'float64'}",
                    "projection",
                    b"x" * (DEFAULT_INLINE_ARTIFACT_THRESHOLD + 1),
                )
            )

        self.assertIsInstance(
            wrapper.carrier.handle,
            ObjectRefArtifactHandle,
        )
        self.assertEqual(wrapper.carrier.handle.reference, reference)
        fake_ray.put.assert_called_once()
        references = driver_artifact_references()
        self.assertEqual(len(references), 1)
        self.assertEqual(
            references[0].content_sha256,
            wrapper.carrier.handle.content_sha256,
        )
        self.assertIs(references[0].reference, reference)
        restored = pickle.loads(pickle.dumps(wrapper))
        self.assertEqual(restored.carrier.handle.reference, reference)

    def test_auto_mode_delegates_to_process_local_adapter_without_fallback(self):
        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize("{'x': 'float64'}", "projection", b"artifact")
        adapter = _Adapter(result=42)
        wrapper._worker_adapter = adapter

        with mock.patch.dict(os.environ, {"UDFJIT_MODE": "auto"}):
            result = wrapper(21)

        self.assertEqual(result, 42)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(original.calls, 0)

    def test_auto_mode_never_replays_original_after_adapter_entry_failure(self):
        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize("{'x': 'float64'}", "projection", b"artifact")
        wrapper._worker_adapter = _Adapter(
            failure=ArithmeticError("post-entry-failure")
        )

        with mock.patch.dict(os.environ, {"UDFJIT_MODE": "auto"}):
            with self.assertRaisesRegex(ArithmeticError, "post-entry-failure"):
                wrapper(21)

        self.assertEqual(original.calls, 0)

    def test_auto_string_schema_uses_typed_loop_result(self):
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            TypedLoopInvocation,
        )

        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize(
            "{'text': 'String', 'unrelated': 'Float64'}",
            "filter",
            invocation_layout=exact_unicode_layout(),
        )
        adapter = _TypedAdapter(TypedLoopInvocation(True, 42, "typed_loop_hit"))
        wrapper._typed_loop_adapter = adapter

        with mock.patch.dict(
            os.environ,
            {
                "UDFJIT_MODE": "auto",
                "UDFJIT_CLUSTER_EPOCH": "layout-epoch",
            },
        ):
            result = wrapper(21)

        self.assertEqual(result, 42)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(original.calls, 0)

    def test_exact_unicode_constructs_adapter_on_first_wrapper_call(self):
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            TypedLoopInvocation,
        )

        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize(
            "{'text': 'String'}",
            "projection",
            invocation_layout=exact_unicode_layout(),
        )
        adapter = _TypedAdapter(
            TypedLoopInvocation(True, "compiled", "typed_loop_hit")
        )

        with mock.patch.dict(
            os.environ,
            {
                "UDFJIT_MODE": "auto",
                "UDFJIT_CLUSTER_EPOCH": "layout-epoch",
            },
        ):
            with mock.patch(
                "python_udf_jit.integration.daft_ray.typed_loop_worker."
                "build_worker_typed_loop_adapter",
                return_value=adapter,
            ) as build:
                result = wrapper("a")

        self.assertEqual(result, "compiled")
        build.assert_called_once_with(wrapper)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(original.calls, 0)

    def test_auto_string_schema_falls_back_once_when_typed_loop_declines(self):
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            TypedLoopInvocation,
        )

        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize(
            "{'text': 'String'}",
            "filter",
            invocation_layout=exact_unicode_layout(),
        )
        adapter = _TypedAdapter(
            TypedLoopInvocation(False, reason_code="predicate_unsupported")
        )
        wrapper._typed_loop_adapter = adapter

        with mock.patch.dict(
            os.environ,
            {
                "UDFJIT_MODE": "auto",
                "UDFJIT_CLUSTER_EPOCH": "layout-epoch",
            },
        ):
            result = wrapper(21)

        self.assertEqual(result, 42)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(original.calls, 1)

    def test_auto_string_layout_epoch_mismatch_skips_adapter_construction(self):
        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize(
            "{'text': 'String'}",
            "filter",
            invocation_layout=exact_unicode_layout(),
        )
        with mock.patch.dict(
            os.environ,
            {
                "UDFJIT_MODE": "auto",
                "UDFJIT_CLUSTER_EPOCH": "other-epoch",
            },
        ):
            with mock.patch(
                "python_udf_jit.integration.daft_ray.typed_loop_worker."
                "build_worker_typed_loop_adapter",
            ) as build:
                result = wrapper(21)

        self.assertEqual(result, 42)
        build.assert_not_called()
        self.assertEqual(original.calls, 1)

    def test_terminal_typed_decline_bypasses_future_adapter_and_events(self):
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            TypedLoopInvocation,
        )

        original = CountingCallable()
        wrapper = make_wrapper(original)
        wrapper.finalize(
            "{'text': 'String'}",
            "filter",
            invocation_layout=exact_unicode_layout(),
        )
        adapter = _TypedAdapter(
            TypedLoopInvocation(
                False,
                reason_code="predicate_unsupported",
                terminal=True,
            )
        )
        wrapper._typed_loop_adapter = adapter

        with mock.patch.dict(
            os.environ,
            {
                "UDFJIT_MODE": "auto",
                "UDFJIT_CLUSTER_EPOCH": "layout-epoch",
            },
        ):
            with mock.patch(
                "python_udf_jit.integration.daft_ray.wrapper.events.try_emit"
            ) as emit:
                self.assertEqual(wrapper(21), 42)
                self.assertEqual(wrapper(22), 44)

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(original.calls, 2)
        emit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
