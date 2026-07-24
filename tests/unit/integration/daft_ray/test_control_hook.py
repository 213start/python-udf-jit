from __future__ import annotations

import importlib
import functools
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.bootstrap import install_post_import_hook
from python_udf_jit.diagnostics.events import clear_events, snapshot_events
from python_udf_jit.integration.daft_ray.compatibility import (
    callable_fingerprint,
    target_for_objects,
)
from python_udf_jit.integration.daft_ray.control import (
    HookStatus,
    install_daft_control_hooks,
    uninstall_daft_control_hooks,
)
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper


MANIFEST_SHA256 = "a" * 64
ROOT = Path(__file__).resolve().parents[4]


def add_one(_instance: object, value: int) -> int:
    return value + 1


def affine(value: float) -> float:
    return value * 2.0 + 3.0


@functools.wraps(affine)
def daft_affine_method(_instance: object, value: float) -> float:
    return affine(value)


class FakeExpression:
    def __init__(self, worker_callable=None):
        self.worker_callable = worker_callable


class FakeFunc:
    def __init__(self, method=add_one):
        self._method = method
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


class FakeDataFrame:
    def __init__(self):
        self.original_with_columns_count = 0

    def schema(self):
        return {"value": "int64"}

    def with_column(self, name, expression):
        return self.with_columns({name: expression})

    def with_columns(self, columns):
        self.original_with_columns_count += 1
        return ("dataframe", columns)


class FakeFloatDataFrame(FakeDataFrame):
    def schema(self):
        return {"value": "float64"}


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
        clear_events()

    def tearDown(self):
        uninstall_daft_control_hooks(FakeFunc, FakeDataFrame)
        uninstall_daft_control_hooks(RaisingFakeFunc, FakeDataFrame)
        clear_events()

    def test_off_mode_leaves_framework_methods_untouched(self):
        original_call = FakeFunc.__call__
        original_with_columns = FakeDataFrame.with_columns

        result, _ = install(mode="off")

        self.assertEqual(result.status, HookStatus.DISABLED)
        self.assertIs(FakeFunc.__call__, original_call)
        self.assertIs(FakeDataFrame.with_columns, original_with_columns)

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
        self.assertEqual(wrapper.logical_schema, "{'value': 'int64'}")

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

    def test_events_distinguish_adapter_finalize_and_fallback(self):
        install()
        expression = FakeFunc()(FakeExpression())
        FakeDataFrame().with_column("answer", expression)
        expression.worker_callable(None, 1)

        stages = [event.stage for event in snapshot_events()]
        self.assertEqual(stages, ["adapter", "adapter", "execute"])
        self.assertNotIn("jit", stages)


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


if __name__ == "__main__":
    unittest.main()
