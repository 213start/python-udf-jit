from __future__ import annotations

import operator
import os
import pickle
import unittest
from unittest import mock

from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper


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


def make_wrapper(original=operator.add):
    return FallbackOnlyWrapper(
        candidate_id="candidate-test",
        original_callable=original,
        carrier=ProductionCarrierState.placeholder("candidate-test", "b" * 64),
    )


class WrapperSerializationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
