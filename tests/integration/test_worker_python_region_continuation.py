from __future__ import annotations

import contextlib
import functools
import io
import os
import unittest
from unittest import mock

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.diagnostics.report import InMemoryRuntimeReport
from python_udf_jit.compiler.reference import reference_resume_semantic
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry
from python_udf_jit.integration.daft_ray.worker import (
    RuntimeTarget,
    WorkerRuntimeContext,
    WorkerScalarAdapter,
)
from python_udf_jit.runtime.variant import WorkerProcessKey
from tests.unit.integration.daft_ray.test_control_hook import (
    FakeExpression,
    FakeFloatDataFrame,
    FakeFunc,
    daft_opaque_middle_method,
    opaque_middle,
)
from tests.unit.integration.daft_ray.test_worker_runtime import (
    _LocalProviderFactory,
)

MANIFEST_SHA256 = "a" * 64


class RegionFailure(RuntimeError):
    pass


class WorkerPythonRegionContinuationIntegrationTest(unittest.TestCase):
    def _build_adapter(self):
        fallback_calls = []

        @functools.wraps(opaque_middle)
        def daft_method(_self, value):
            fallback_calls.append(value)
            return opaque_middle(value)

        registry = CandidateRegistry(MANIFEST_SHA256)
        func = FakeFunc(daft_opaque_middle_method)
        record = registry.register(func, daft_method)
        expression = FakeExpression(record.wrapper)
        registry.bind_expression(expression, record)
        self.assertEqual(
            registry.finalize_operation(
                FakeFloatDataFrame(),
                "with_columns",
                ({"result": expression},),
                {},
            ),
            1,
        )
        wrapper = record.wrapper
        self.assertTrue(wrapper.carrier.finalized)
        self.assertGreater(len(wrapper.carrier.artifact_bytes), 0)
        process = WorkerProcessKey(
            "epoch-region",
            "node-region",
            "worker-region",
            os.getpid(),
            "generation-region",
        )
        report = InMemoryRuntimeReport()
        provider = _LocalProviderFactory()
        adapter = WorkerScalarAdapter(
            candidate_id=record.candidate_id,
            original_callable=daft_method,
            carrier=wrapper.carrier,
            logical_schema=wrapper.logical_schema or "",
            context=WorkerRuntimeContext(
                "run-region",
                process,
                "partition-region",
                "attempt-region",
            ),
            target_provider=lambda: RuntimeTarget(
                "3.14.3",
                "cpython-314-aarch64-linux-gnu",
                ("asimd",),
            ),
            provider_factory=provider,
            event_sink=report,
        )
        return adapter, provider, report, fallback_calls

    def test_driver_artifact_worker_executes_each_stage_once(self):
        adapter, provider, report, fallback_calls = self._build_adapter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with (
                mock.patch(
                    (
                        "python_udf_jit.integration.daft_ray.worker."
                        "analyze_function"
                    ),
                    wraps=analyze_function,
                ) as analyze_call,
                mock.patch(
                    (
                        "python_udf_jit.integration.daft_ray.worker."
                        "reference_resume_semantic"
                    ),
                    wraps=reference_resume_semantic,
                ) as suffix_call,
            ):
                adapter.invoke((None, 2.0), {})
                fallback_calls.clear()
                output.seek(0)
                output.truncate(0)
                self.assertEqual(
                    adapter.invoke((None, 3.0), {}),
                    7.0,
                )
                self.assertEqual(
                    adapter.invoke((None, 4.0), {}),
                    9.0,
                )

        self.assertEqual(output.getvalue(), "6.0\n8.0\n")
        self.assertEqual(analyze_call.call_count, 1)
        self.assertEqual(suffix_call.call_count, 2)
        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(provider.continuation_payload_count, 2)
        self.assertEqual(
            provider.continuation_payload_values,
            [(6.0,), (8.0,)],
        )
        self.assertEqual(fallback_calls, [])
        self.assertEqual(
            sum(
                event.decision == "semantic_execute"
                for event in report.snapshot()
            ),
            2,
        )

    def test_region_exception_propagates_once_without_whole_function_replay(
        self,
    ):
        adapter, provider, report, fallback_calls = self._build_adapter()
        with contextlib.redirect_stdout(io.StringIO()):
            adapter.invoke((None, 2.0), {})
        fallback_calls.clear()
        failure = RegionFailure("opaque-region-failure")

        with (
            mock.patch("builtins.print", side_effect=failure) as opaque_call,
            mock.patch(
                (
                    "python_udf_jit.integration.daft_ray.worker."
                    "reference_resume_semantic"
                ),
                wraps=reference_resume_semantic,
            ) as suffix_call,
        ):
            with self.assertRaises(RegionFailure) as raised:
                adapter.invoke((None, 3.0), {})

        self.assertIs(raised.exception, failure)
        opaque_call.assert_called_once_with(6.0)
        suffix_call.assert_not_called()
        self.assertEqual(provider.compile_count, 1)
        self.assertEqual(provider.continuation_payload_count, 1)
        self.assertEqual(
            provider.continuation_payload_values,
            [(6.0,)],
        )
        self.assertEqual(fallback_calls, [])
        self.assertTrue(
            any(
                event.decision == "post_entry_failure"
                and event.reason_code == "RegionFailure"
                for event in report.snapshot()
            )
        )


if __name__ == "__main__":
    unittest.main()
