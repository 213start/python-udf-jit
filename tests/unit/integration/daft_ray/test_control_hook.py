from __future__ import annotations

import importlib
import functools
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.bootstrap import install_post_import_hook
from python_udf_jit.compiler.capture import try_capture
from python_udf_jit.diagnostics.events import clear_events, snapshot_events
from python_udf_jit.integration.daft_ray.compatibility import (
    callable_fingerprint,
    target_for_objects,
)
from python_udf_jit.integration.daft_ray.control import (
    HookResult,
    HookStatus,
    install_daft_control_hooks,
    install_default_daft_hooks,
    uninstall_daft_control_hooks,
)
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper
from python_udf_jit.protocol.codec import decode_artifact


MANIFEST_SHA256 = "a" * 64
ROOT = Path(__file__).resolve().parents[4]


def add_one(_instance: object, value: int) -> int:
    return value + 1


def affine(value: float) -> float:
    return value * 2.0 + 3.0


def opaque_middle(value: float) -> float:
    prefix = value * 2.0
    print(prefix)
    return prefix + 1.0


def unsupported_opaque_middle(value: float) -> float:
    prefix = value * 2.0
    print(prefix)
    return prefix / 1.0


@functools.wraps(affine)
def daft_affine_method(_instance: object, value: float) -> float:
    return affine(value)


@functools.wraps(opaque_middle)
def daft_opaque_middle_method(
    _instance: object,
    value: float,
) -> float:
    return opaque_middle(value)


@functools.wraps(unsupported_opaque_middle)
def daft_unsupported_opaque_middle_method(
    _instance: object,
    value: float,
) -> float:
    return unsupported_opaque_middle(value)


class FakePyExpr:
    def _hash(self):
        return id(self)


class FakeExpression:
    def __init__(self, worker_callable=None):
        self.worker_callable = worker_callable
        self._expr = FakePyExpr()


class FakeComposedExpression(FakeExpression):
    def __init__(self, child):
        super().__init__()
        self.child = child


class FakeFunc:
    def __init__(
        self,
        method=add_one,
        *,
        on_error="raise",
        max_retries=2,
        use_process=True,
    ):
        self._method = method
        self.on_error = on_error
        self.max_retries = max_retries
        self.use_process = use_process
        self.original_call_count = 0

    def __call__(self, *args, **kwargs):
        self.original_call_count += 1
        if any(isinstance(value, FakeExpression) for value in (*args, *kwargs.values())):
            return FakeExpression(self._method)
        return self._method(None, *args, **kwargs)


class RaisingFakeFunc(FakeFunc):
    def __call__(self, *args, **kwargs):
        self.original_call_count += 1
        raise LookupError("daft-original-error")


class RecursiveFakeFunc(FakeFunc):
    def __init__(self):
        super().__init__()
        self.recursing = False

    def __call__(self, *args, **kwargs):
        if not self.recursing:
            self.recursing = True
            try:
                return self(*args, **kwargs)
            finally:
                self.recursing = False
        return super().__call__(*args, **kwargs)


class FakeDataFrame:
    def __init__(self):
        self.original_with_columns_count = 0
        self.original_where_count = 0
        self.original_select_count = 0

    def schema(self):
        return {"value": "int64"}

    def with_column(self, name, expression):
        return self.with_columns({name: expression})

    def with_columns(self, columns):
        self.original_with_columns_count += 1
        return ("dataframe", columns)

    def where(self, predicate):
        self.original_where_count += 1
        return ("where", predicate)

    def select(self, *columns, **projections):
        self.original_select_count += 1
        return ("select", columns, projections)


class FakeFloatDataFrame(FakeDataFrame):
    def schema(self):
        return {"value": "float64"}


class InvalidSchemaDataFrame(FakeDataFrame):
    def schema(self):
        return {"value": object()}


class BrokenRegistry(CandidateRegistry):
    def register(self, func, original_callable):
        raise RuntimeError("registry unavailable")


def install(*, mode="observe", registry=None, target=None, func_class=FakeFunc):
    registry = registry or CandidateRegistry(MANIFEST_SHA256)
    target = target or target_for_objects(
        SimpleNamespace(__version__="0.7.2"), func_class, FakeDataFrame
    )
    result = install_daft_control_hooks(
        daft_module=SimpleNamespace(__version__="0.7.2"),
        func_class=func_class,
        dataframe_class=FakeDataFrame,
        expression_class=FakeExpression,
        mode=mode,
        registry=registry,
        target=target,
    )
    return result, registry


class ControlHookTest(unittest.TestCase):
    def setUp(self):
        uninstall_daft_control_hooks(FakeFunc, FakeDataFrame)
        uninstall_daft_control_hooks(RaisingFakeFunc, FakeDataFrame)
        uninstall_daft_control_hooks(RecursiveFakeFunc, FakeDataFrame)
        clear_events()

    def tearDown(self):
        uninstall_daft_control_hooks(FakeFunc, FakeDataFrame)
        uninstall_daft_control_hooks(RaisingFakeFunc, FakeDataFrame)
        uninstall_daft_control_hooks(RecursiveFakeFunc, FakeDataFrame)
        clear_events()

    def test_off_mode_leaves_framework_methods_untouched(self):
        original_call = FakeFunc.__call__
        original_with_columns = FakeDataFrame.with_columns

        result, _ = install(mode="off")

        self.assertEqual(result.status, HookStatus.DISABLED)
        self.assertIs(FakeFunc.__call__, original_call)
        self.assertIs(FakeDataFrame.with_columns, original_with_columns)

    def test_default_install_requires_exact_objectref_bridge(self):
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "UDFJIT_MODE": "auto",
                    "UDFJIT_MANIFEST_SHA256": MANIFEST_SHA256,
                },
                clear=False,
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control.install_daft_objectref_bridge",
                return_value=SimpleNamespace(
                    installed=False,
                    reason="objectref_bridge_contract_mismatch",
                ),
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control.importlib.import_module",
                return_value=SimpleNamespace(),
            ) as import_module,
        ):
            result = install_default_daft_hooks(
                SimpleNamespace(__version__="0.7.2")
            )

        self.assertEqual(result.status, HookStatus.ERROR)
        self.assertEqual(
            result.reason,
            "objectref_bridge_contract_mismatch",
        )
        import_module.assert_called_once_with(
            "daft.runners.flotilla"
        )

    def test_default_install_off_diagnostics_skips_policy_resolution(self):
        registry = object()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "UDFJIT_MODE": "auto",
                    "UDFJIT_MANIFEST_SHA256": MANIFEST_SHA256,
                    "UDFJIT_DIAGNOSTICS": "off",
                },
                clear=True,
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control.Path.cwd",
                side_effect=AssertionError("normal path resolved cwd"),
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control."
                "resolve_diagnostic_policy",
                side_effect=AssertionError("normal path parsed diagnostics"),
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control."
                "install_daft_objectref_bridge",
                return_value=SimpleNamespace(installed=True, reason="installed"),
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control.CandidateRegistry",
                return_value=registry,
            ) as registry_class,
            mock.patch(
                "python_udf_jit.integration.daft_ray.control."
                "install_daft_control_hooks",
                return_value=HookResult(
                    HookStatus.INSTALLED,
                    "compatible_hooks_installed",
                ),
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control._DEFAULT_REGISTRY",
                None,
            ),
            mock.patch(
                "python_udf_jit.integration.daft_ray.control."
                "importlib.import_module",
                side_effect=(
                    SimpleNamespace(),
                    SimpleNamespace(Func=FakeFunc),
                    SimpleNamespace(DataFrame=FakeDataFrame),
                    SimpleNamespace(Expression=FakeExpression),
                ),
            ),
        ):
            result = install_default_daft_hooks(
                SimpleNamespace(__version__="0.7.2")
            )

        self.assertEqual(result.status, HookStatus.INSTALLED)
        _, keyword_arguments = registry_class.call_args
        self.assertFalse(keyword_arguments["diagnostic_policy"].enabled)

    def test_default_install_freezes_diagnostic_policy_into_registry(self):
        registry = object()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "UDFJIT_MODE": "auto",
                "UDFJIT_MANIFEST_SHA256": MANIFEST_SHA256,
                "UDFJIT_RUN_ID": "run-a",
                "UDFJIT_DIAGNOSTICS": "summary",
                "UDFJIT_DIAGNOSTIC_DIR": str(root / "diagnostics"),
                "UDFJIT_DIAGNOSTIC_FILTER": "candidate:candidate-a",
                "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
                "UDFJIT_DIAGNOSTIC_PERF": "off",
                "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
                "UDFJIT_DIAGNOSTIC_MAX_BYTES": "1048576",
            }
            with (
                mock.patch.dict("os.environ", environment, clear=False),
                mock.patch(
                    "python_udf_jit.integration.daft_ray.control."
                    "install_daft_objectref_bridge",
                    return_value=SimpleNamespace(
                        installed=True,
                        reason="installed",
                    ),
                ),
                mock.patch(
                    "python_udf_jit.integration.daft_ray.control.CandidateRegistry",
                    return_value=registry,
                ) as registry_class,
                mock.patch(
                    "python_udf_jit.integration.daft_ray.control."
                    "install_daft_control_hooks",
                    return_value=HookResult(
                        HookStatus.INSTALLED,
                        "compatible_hooks_installed",
                    ),
                ),
                mock.patch(
                    "python_udf_jit.integration.daft_ray.control._DEFAULT_REGISTRY",
                    None,
                ),
                mock.patch(
                    "python_udf_jit.integration.daft_ray.control."
                    "importlib.import_module",
                    side_effect=(
                        SimpleNamespace(),
                        SimpleNamespace(Func=FakeFunc),
                        SimpleNamespace(DataFrame=FakeDataFrame),
                        SimpleNamespace(Expression=FakeExpression),
                    ),
                ),
            ):
                result = install_default_daft_hooks(
                    SimpleNamespace(__version__="0.7.2")
                )

        self.assertEqual(result.status, HookStatus.INSTALLED)
        _, keyword_arguments = registry_class.call_args
        self.assertTrue(keyword_arguments["diagnostic_policy"].enabled)
        self.assertEqual(keyword_arguments["diagnostic_run_id"], "run-a")
        self.assertEqual(keyword_arguments["diagnostic_runtime_mode"], "auto")
        self.assertTrue(
            keyword_arguments["diagnostic_process_key"].startswith("driver-")
        )

    def test_fingerprint_mismatch_fails_open_without_patching(self):
        original_call = FakeFunc.__call__
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"), FakeFunc, FakeDataFrame
        )
        target = target._replace(func_call_fingerprint="0" * 64)

        result, _ = install(target=target)

        self.assertEqual(result.status, HookStatus.INCOMPATIBLE)
        self.assertIs(FakeFunc.__call__, original_call)

    def test_repeated_install_is_idempotent_and_registers_once(self):
        first, registry = install()
        second, _ = install(registry=registry)
        func = FakeFunc()
        expression = func(FakeExpression())

        self.assertEqual(first.status, HookStatus.INSTALLED)
        self.assertEqual(second.status, HookStatus.ALREADY_INSTALLED)
        self.assertIsInstance(expression.worker_callable, FallbackOnlyWrapper)
        self.assertEqual(func.original_call_count, 1)
        self.assertEqual(registry.registration_count, 1)

    def test_func_method_is_only_temporarily_replaced(self):
        _, registry = install()
        func = FakeFunc()
        original_method = func._method

        expression = func(FakeExpression())

        self.assertIs(func._method, original_method)
        self.assertIsInstance(expression.worker_callable, FallbackOnlyWrapper)
        self.assertIs(expression.worker_callable.original_callable, original_method)
        self.assertEqual(expression.worker_callable(None, 41), 42)
        self.assertEqual(registry.registration_count, 1)

    def test_with_column_delegation_finalizes_exactly_once(self):
        _, registry = install()
        expression = FakeFunc()(FakeExpression())
        dataframe = FakeDataFrame()

        result = dataframe.with_column("answer", expression)

        wrapper = expression.worker_callable
        self.assertEqual(result[0], "dataframe")
        self.assertEqual(dataframe.original_with_columns_count, 1)
        self.assertEqual(registry.finalization_count, 1)
        self.assertEqual(wrapper.usage_context, "projection")
        schema = json.loads(wrapper.logical_schema)
        self.assertEqual(schema["schema_version"], 1)
        self.assertEqual(
            schema["fields"][0]["logical_type"],
            "int64",
        )

    def test_where_select_and_with_columns_finalize_each_candidate_once(self):
        _, registry = install()
        dataframe = FakeDataFrame()
        projection = FakeFunc()(FakeExpression())
        predicate = FakeFunc()(FakeExpression())
        selected = FakeFunc()(FakeExpression())

        dataframe.with_columns({"projected": projection})
        dataframe.where(predicate)
        dataframe.select(selected, duplicate=selected)

        self.assertEqual(registry.finalization_count, 3)
        self.assertEqual(projection.worker_callable.usage_context, "projection")
        self.assertEqual(predicate.worker_callable.usage_context, "filter")
        self.assertEqual(selected.worker_callable.usage_context, "selection")
        self.assertEqual(dataframe.original_with_columns_count, 1)
        self.assertEqual(dataframe.original_where_count, 1)
        self.assertEqual(dataframe.original_select_count, 1)

    def test_serialized_expression_lineage_finds_nested_candidate(self):
        _, registry = install()
        direct = FakeFunc()(FakeExpression())
        nested = FakeComposedExpression(direct)

        FakeDataFrame().select(nested)

        self.assertEqual(registry.finalization_count, 1)
        self.assertEqual(direct.worker_callable.usage_context, "selection")

    def test_udf_options_and_method_are_stable_across_concurrent_calls(self):
        _, registry = install()
        func = FakeFunc(on_error="log", max_retries=7, use_process=False)
        original_method = func._method
        before = (func.on_error, func.max_retries, func.use_process)

        with ThreadPoolExecutor(max_workers=8) as executor:
            expressions = list(
                executor.map(
                    lambda _index: func(FakeExpression()),
                    range(32),
                )
            )

        self.assertIs(func._method, original_method)
        self.assertEqual(
            (func.on_error, func.max_retries, func.use_process),
            before,
        )
        self.assertEqual(registry.registration_count, 1)
        self.assertEqual(func.original_call_count, 32)
        self.assertTrue(
            all(
                isinstance(expression.worker_callable, FallbackOnlyWrapper)
                for expression in expressions
            )
        )

    def test_supported_float64_candidate_finalizes_a_real_inline_artifact(self):
        registry = CandidateRegistry(MANIFEST_SHA256)
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"), FakeFunc, FakeFloatDataFrame
        )
        result = install_daft_control_hooks(
            daft_module=SimpleNamespace(__version__="0.7.2"),
            func_class=FakeFunc,
            dataframe_class=FakeFloatDataFrame,
            expression_class=FakeExpression,
            mode="auto",
            registry=registry,
            target=target,
        )
        self.assertEqual(result.status, HookStatus.INSTALLED)
        try:
            expression = FakeFunc(daft_affine_method)(FakeExpression())
            FakeFloatDataFrame().with_column("result", expression)
            wrapper = expression.worker_callable

            self.assertTrue(wrapper.carrier.finalized)
            self.assertGreater(len(wrapper.carrier.artifact_bytes), 0)
            record = registry.records()[0]
            self.assertIsNotNone(record.semantic_core_hash)
            self.assertIsNotNone(record.semantic_region_hash)
            self.assertEqual(len(record.semantic_core_hash), 64)
            self.assertEqual(len(record.semantic_region_hash), 64)
        finally:
            uninstall_daft_control_hooks(FakeFunc, FakeFloatDataFrame)

    def test_real_scalar_graph_break_finalizes_three_region_artifact(self):
        registry = CandidateRegistry(MANIFEST_SHA256)
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"),
            FakeFunc,
            FakeFloatDataFrame,
        )
        result = install_daft_control_hooks(
            daft_module=SimpleNamespace(__version__="0.7.2"),
            func_class=FakeFunc,
            dataframe_class=FakeFloatDataFrame,
            expression_class=FakeExpression,
            mode="auto",
            registry=registry,
            target=target,
        )
        self.assertEqual(result.status, HookStatus.INSTALLED)
        try:
            expression = FakeFunc(
                daft_opaque_middle_method
            )(FakeExpression())
            FakeFloatDataFrame().with_column("result", expression)
            wrapper = expression.worker_callable
            artifact = decode_artifact(
                wrapper.carrier.artifact_bytes
            )

            self.assertTrue(wrapper.carrier.finalized)
            self.assertEqual(
                tuple(
                    region.provider_candidates
                    for region in artifact.semantic_region_graph.regions
                ),
                (
                    ("scalar_cinderx",),
                    (),
                    ("scalar_cinderx",),
                ),
            )
            self.assertEqual(
                len(artifact.semantic_core_module.python_regions),
                1,
            )
            record = registry.records()[0]
            self.assertIsNone(record.capture_ir)
            self.assertIsNotNone(record.semantic_core_hash)
            self.assertIsNotNone(record.semantic_region_hash)
        finally:
            uninstall_daft_control_hooks(FakeFunc, FakeFloatDataFrame)

    def test_unproven_graph_break_shape_falls_back_before_artifact_commit(self):
        registry = CandidateRegistry(MANIFEST_SHA256)
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"),
            FakeFunc,
            FakeFloatDataFrame,
        )
        result = install_daft_control_hooks(
            daft_module=SimpleNamespace(__version__="0.7.2"),
            func_class=FakeFunc,
            dataframe_class=FakeFloatDataFrame,
            expression_class=FakeExpression,
            mode="auto",
            registry=registry,
            target=target,
        )
        self.assertEqual(result.status, HookStatus.INSTALLED)
        try:
            expression = FakeFunc(
                daft_unsupported_opaque_middle_method
            )(FakeExpression())
            FakeFloatDataFrame().with_column("result", expression)
            wrapper = expression.worker_callable
            record = registry.records()[0]

            self.assertFalse(wrapper.carrier.finalized)
            self.assertEqual(
                wrapper.carrier.handle.kind,
                "placeholder",
            )
            self.assertEqual(wrapper.carrier.handle.size_bytes, 0)
            self.assertIsNone(record.capture_ir)
            self.assertIsNone(record.semantic_core_hash)
            self.assertIsNone(record.semantic_region_hash)
            with mock.patch("builtins.print") as opaque_call:
                self.assertEqual(wrapper(None, 3.0), 6.0)
            opaque_call.assert_called_once_with(6.0)
        finally:
            uninstall_daft_control_hooks(FakeFunc, FakeFloatDataFrame)

    def test_capture_is_deferred_until_operation_schema_is_available(self):
        registry = CandidateRegistry(MANIFEST_SHA256)
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"), FakeFunc, FakeFloatDataFrame
        )
        result = install_daft_control_hooks(
            daft_module=SimpleNamespace(__version__="0.7.2"),
            func_class=FakeFunc,
            dataframe_class=FakeFloatDataFrame,
            expression_class=FakeExpression,
            mode="auto",
            registry=registry,
            target=target,
        )
        self.assertEqual(result.status, HookStatus.INSTALLED)
        try:
            func = FakeFunc(daft_affine_method)
            with mock.patch(
                "python_udf_jit.integration.daft_ray.registry.try_capture",
                wraps=try_capture,
            ) as capture:
                expression = func(FakeExpression())
                capture.assert_not_called()

                FakeFloatDataFrame().with_columns({"result": expression})

            capture.assert_called_once()
            self.assertIs(capture.call_args.args[0].callable_object, func)
        finally:
            uninstall_daft_control_hooks(FakeFunc, FakeFloatDataFrame)

    def test_registry_failure_calls_original_framework_method_once(self):
        registry = BrokenRegistry(MANIFEST_SHA256)
        install(registry=registry)
        func = FakeFunc()
        original_method = func._method

        expression = func(FakeExpression())

        self.assertEqual(func.original_call_count, 1)
        self.assertIs(expression.worker_callable, original_method)
        self.assertIs(func._method, original_method)

    def test_original_framework_exception_is_not_retried_or_replaced(self):
        install(func_class=RaisingFakeFunc)
        func = RaisingFakeFunc()
        original_method = func._method

        with self.assertRaisesRegex(LookupError, "daft-original-error"):
            func(FakeExpression())

        self.assertEqual(func.original_call_count, 1)
        self.assertIs(func._method, original_method)

    def test_recursive_hook_entry_uses_original_call_and_restores_method(self):
        install(func_class=RecursiveFakeFunc)
        func = RecursiveFakeFunc()
        original_method = func._method

        expression = func(FakeExpression())

        self.assertIs(func._method, original_method)
        self.assertIsInstance(expression.worker_callable, FallbackOnlyWrapper)
        self.assertEqual(func.original_call_count, 1)

    def test_invalid_schema_fails_open_to_original_dataframe_operation(self):
        registry = CandidateRegistry(MANIFEST_SHA256)
        target = target_for_objects(
            SimpleNamespace(__version__="0.7.2"),
            FakeFunc,
            InvalidSchemaDataFrame,
        )
        result = install_daft_control_hooks(
            daft_module=SimpleNamespace(__version__="0.7.2"),
            func_class=FakeFunc,
            dataframe_class=InvalidSchemaDataFrame,
            expression_class=FakeExpression,
            mode="observe",
            registry=registry,
            target=target,
        )
        self.assertEqual(result.status, HookStatus.INSTALLED)
        try:
            expression = FakeFunc()(FakeExpression())
            dataframe = InvalidSchemaDataFrame()

            returned = dataframe.with_columns({"result": expression})

            self.assertEqual(returned[0], "dataframe")
            self.assertEqual(dataframe.original_with_columns_count, 1)
            self.assertEqual(registry.finalization_count, 0)
        finally:
            uninstall_daft_control_hooks(FakeFunc, InvalidSchemaDataFrame)

    def test_events_distinguish_adapter_finalize_and_fallback(self):
        install()
        expression = FakeFunc()(FakeExpression())
        FakeDataFrame().with_column("answer", expression)
        expression.worker_callable(None, 1)

        stages = [event.stage for event in snapshot_events()]
        self.assertEqual(stages, ["adapter", "adapter", "execute"])
        self.assertNotIn("jit", stages)

    def test_registry_ttl_and_job_cleanup_remove_candidate_lineage(self):
        now = [0.0]
        registry = CandidateRegistry(
            MANIFEST_SHA256,
            job_namespace="job-a",
            ttl_seconds=5.0,
            clock=lambda: now[0],
        )
        install(registry=registry)
        func = FakeFunc()
        expression = func(FakeExpression())

        now[0] = 6.0

        self.assertEqual(registry.purge_expired(), 1)
        self.assertEqual(registry.records(), ())
        self.assertEqual(
            registry.finalize_operation(
                FakeDataFrame(),
                "with_columns",
                ({"answer": expression},),
                {},
            ),
            0,
        )

        registry.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            registry.records()

    def test_registry_lru_eviction_removes_expression_lineage(self):
        registry = CandidateRegistry(MANIFEST_SHA256, max_candidates=1)
        install(registry=registry)
        first_func = FakeFunc()
        first = first_func(FakeExpression())
        second_func = FakeFunc()
        second = second_func(FakeExpression())

        self.assertEqual(len(registry.records()), 1)
        self.assertEqual(
            registry.finalize_operation(
                FakeDataFrame(),
                "with_columns",
                ({"first": first},),
                {},
            ),
            0,
        )
        self.assertEqual(
            registry.finalize_operation(
                FakeDataFrame(),
                "with_columns",
                ({"second": second},),
                {},
            ),
            1,
        )


class CompatibilityFingerprintTest(unittest.TestCase):
    def test_docstrings_do_not_change_semantic_fingerprint(self):
        source_with_unicode_doc = '''
def target(value):
    """Unicode documentation: ╭─╮"""
    return value + 1
'''
        source_with_other_doc = '''
def target(value):
    """Different documentation."""
    return value + 1
'''

        with mock.patch(
            "python_udf_jit.integration.daft_ray.compatibility.inspect.getsource",
            side_effect=(source_with_unicode_doc, source_with_other_doc),
        ):
            first = callable_fingerprint(object())
            second = callable_fingerprint(object())

        self.assertEqual(first, second)

    def test_executable_body_change_changes_fingerprint(self):
        first_source = "def target(value):\n    return value + 1\n"
        second_source = "def target(value):\n    return value + 2\n"

        with mock.patch(
            "python_udf_jit.integration.daft_ray.compatibility.inspect.getsource",
            side_effect=(first_source, second_source),
        ):
            first = callable_fingerprint(object())
            second = callable_fingerprint(object())

        self.assertNotEqual(first, second)


class PostImportHookTest(unittest.TestCase):
    def test_image_pth_is_lightweight_and_calls_environment_bootstrap(self):
        pth = (
            ROOT / "docker/scalar-piercing/python-udf-jit-bootstrap.pth"
        ).read_text(encoding="utf-8")

        self.assertEqual(1, len(pth.splitlines()))
        self.assertTrue(pth.startswith("import python_udf_jit.bootstrap"))
        self.assertIn("bootstrap_from_environment()", pth)
        self.assertNotIn("import daft", pth)
        self.assertNotIn("import ray", pth)

    def test_callback_runs_after_target_module_initialization_once(self):
        module_name = "udfjit_fake_daft_post_import"
        observations = []
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, f"{module_name}.py").write_text(
                "INITIALIZED = 'ready'\n", encoding="utf-8"
            )
            sys.path.insert(0, directory)
            try:
                first = install_post_import_hook(
                    module_name, lambda module: observations.append(module.INITIALIZED)
                )
                second = install_post_import_hook(
                    module_name, lambda module: observations.append("duplicate")
                )
                imported = importlib.import_module(module_name)
            finally:
                sys.path.remove(directory)
                sys.modules.pop(module_name, None)
                first.uninstall()
                second.uninstall()

        self.assertEqual(imported.INITIALIZED, "ready")
        self.assertEqual(observations, ["ready"])

    def test_explicit_diagnostics_do_not_swallow_bootstrap_failure(self):
        module_name = "udfjit_fake_daft_diagnostic_failure"
        callback_calls = []

        def fail_diagnostic_bootstrap(_module):
            callback_calls.append("called")
            raise RuntimeError("diagnostic-bootstrap-failed")

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, f"{module_name}.py").write_text(
                "INITIALIZED = 'ready'\n",
                encoding="utf-8",
            )
            sys.path.insert(0, directory)
            hook = None
            try:
                with mock.patch.dict(
                    "os.environ",
                    {"UDFJIT_DIAGNOSTICS": "full"},
                    clear=False,
                ):
                    hook = install_post_import_hook(
                        module_name,
                        fail_diagnostic_bootstrap,
                    )
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "diagnostic-bootstrap-failed",
                        ):
                            importlib.import_module(module_name)
            finally:
                sys.path.remove(directory)
                sys.modules.pop(module_name, None)
                if hook is not None:
                    hook.uninstall()

        self.assertEqual(callback_calls, ["called", "called"])


if __name__ == "__main__":
    unittest.main()
