from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from python_udf_jit.bootstrap_install import install_bootstrap
from python_udf_jit.diagnostics.events import clear_events, snapshot_events
from python_udf_jit.integration.daft_ray.control import (
    HookStatus,
    install_daft_control_hooks,
    uninstall_daft_control_hooks,
)
from python_udf_jit.integration.daft_ray.compatibility import (
    DAFT_V0_7_2_TARGET,
)
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry


def _projection(value: float) -> float:
    return value * 2.0


def _predicate(value: float) -> bool:
    return value >= 2.0


def _selection(value: float) -> float:
    return value + 10.0


def _side_effect_projection(value: float) -> float:
    with Path(os.environ["UDFJIT_U2_SIDE_EFFECT_FILE"]).open(
        "a",
        encoding="utf-8",
    ) as stream:
        stream.write(f"{value!r}\n")
    return value * 3.0


def _raising_projection(value: float) -> float:
    if value == 2.0:
        raise LookupError("u2-real-daft-error")
    return value


class DaftOperationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import daft
            from daft.dataframe.dataframe import DataFrame
            from daft.expressions.expressions import Expression
            from daft.udf.udf_v2 import Func
        except ImportError as error:
            raise unittest.SkipTest("requires Daft 0.7.2") from error
        cls.daft = daft
        cls.DataFrame = DataFrame
        cls.Expression = Expression
        cls.Func = Func
        cls.daft.set_runner_native()

    def setUp(self):
        uninstall_daft_control_hooks(self.Func, self.DataFrame)
        clear_events()

    def tearDown(self):
        uninstall_daft_control_hooks(self.Func, self.DataFrame)
        clear_events()

    def _install(self, mode: str):
        registry = CandidateRegistry(
            "c" * 64,
            job_namespace="real-daft-u2-test",
        )
        result = install_daft_control_hooks(
            daft_module=self.daft,
            func_class=self.Func,
            dataframe_class=self.DataFrame,
            expression_class=self.Expression,
            mode=mode,
            registry=registry,
        )
        return result, registry

    def test_real_where_select_with_columns_preserve_options_and_scalar_semantics(self):
        result, registry = self._install("observe")
        self.assertEqual(result.status, HookStatus.INSTALLED)
        projection = self.daft.func(
            _projection,
            on_error="raise",
            max_retries=2,
            use_process=False,
        )
        predicate = self.daft.func(
            _predicate,
            on_error="raise",
            max_retries=2,
            use_process=False,
        )
        selection = self.daft.func(
            _selection,
            on_error="raise",
            max_retries=2,
            use_process=False,
        )
        functions = (projection, predicate, selection)
        before = tuple(
            tuple(
                getattr(function, field)
                for field in DAFT_V0_7_2_TARGET.func_option_fields
            )
            for function in functions
        )
        projected = projection(self.daft.col("value"))
        filtered = predicate(self.daft.col("value"))
        selected = (
            selection(self.daft.col("value")) + 1.0
        ).alias("selected")

        with mock.patch.dict(os.environ, {"UDFJIT_MODE": "observe"}):
            document = (
                self.daft.from_pydict(
                    {
                        "row_id": [0, 1, 2],
                        "value": [1.0, 2.0, 3.0],
                    }
                )
                .with_columns({"projected": projected})
                .where(filtered)
                .select(
                    "row_id",
                    "projected",
                    selected=selected,
                )
                .to_pydict()
            )

        after = tuple(
            tuple(
                getattr(function, field)
                for field in DAFT_V0_7_2_TARGET.func_option_fields
            )
            for function in functions
        )
        self.assertEqual(document["row_id"], [1, 2])
        self.assertEqual(document["projected"], [4.0, 6.0])
        self.assertEqual(document["selected"], [13.0, 14.0])
        self.assertEqual(before, after)
        self.assertTrue(all(not function.is_batch for function in functions))
        self.assertEqual(registry.registration_count, 3)
        self.assertEqual(registry.finalization_count, 3)
        self.assertTrue(
            all(record.wrapper.scalar_call_view() for record in registry.records())
        )
        reason_codes = [event.reason_code for event in snapshot_events()]
        self.assertFalse(
            any(
                token in reason.lower()
                for reason in reason_codes
                for token in ("batch", "arrow", "vector")
            )
        )

    def test_off_and_observe_match_without_repeating_side_effects(self):
        values = [1.0, 2.0, 3.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = {}
            for mode in ("off", "observe"):
                uninstall_daft_control_hooks(self.Func, self.DataFrame)
                clear_events()
                result, registry = self._install(mode)
                side_effect_file = root / f"{mode}.txt"
                function = self.daft.func(
                    _side_effect_projection,
                    on_error="raise",
                    max_retries=0,
                    use_process=False,
                )
                with mock.patch.dict(
                    os.environ,
                    {
                        "UDFJIT_MODE": mode,
                        "UDFJIT_U2_SIDE_EFFECT_FILE": str(side_effect_file),
                    },
                ):
                    document = (
                        self.daft.from_pydict(
                            {"row_id": [0, 1, 2], "value": values}
                        )
                        .with_columns(
                            {"result": function(self.daft.col("value"))}
                        )
                        .select("row_id", "result")
                        .to_pydict()
                    )
                observations[mode] = (
                    document,
                    side_effect_file.read_text(encoding="utf-8").splitlines(),
                    registry.registration_count,
                )
                if mode == "off":
                    self.assertEqual(result.status, HookStatus.DISABLED)
                else:
                    self.assertEqual(result.status, HookStatus.INSTALLED)

        self.assertEqual(observations["off"][0], observations["observe"][0])
        self.assertEqual(observations["off"][1], ["1.0", "2.0", "3.0"])
        self.assertEqual(observations["observe"][1], ["1.0", "2.0", "3.0"])
        self.assertEqual(observations["off"][2], 0)
        self.assertEqual(observations["observe"][2], 1)

    def test_off_and_observe_preserve_real_daft_exception_semantics(self):
        observations = {}
        for mode in ("off", "observe"):
            uninstall_daft_control_hooks(self.Func, self.DataFrame)
            clear_events()
            result, registry = self._install(mode)
            function = self.daft.func(
                _raising_projection,
                on_error="raise",
                max_retries=0,
                use_process=False,
            )
            before = tuple(
                getattr(function, field)
                for field in DAFT_V0_7_2_TARGET.func_option_fields
            )
            with mock.patch.dict(os.environ, {"UDFJIT_MODE": mode}):
                try:
                    (
                        self.daft.from_pydict(
                            {"row_id": [0, 1, 2], "value": [1.0, 2.0, 3.0]}
                        )
                        .with_columns(
                            {"result": function(self.daft.col("value"))}
                        )
                        .select("row_id", "result")
                        .to_pydict()
                    )
                except BaseException as error:
                    observation = (type(error).__name__, str(error))
                else:
                    self.fail(f"{mode} unexpectedly suppressed the UDF exception")
            after = tuple(
                getattr(function, field)
                for field in DAFT_V0_7_2_TARGET.func_option_fields
            )
            self.assertEqual(before, after)
            observations[mode] = observation
            if mode == "off":
                self.assertEqual(result.status, HookStatus.DISABLED)
                self.assertEqual(registry.registration_count, 0)
            else:
                self.assertEqual(result.status, HookStatus.INSTALLED)
                self.assertEqual(registry.registration_count, 1)

        self.assertEqual(observations["off"][0], observations["observe"][0])
        self.assertIn("u2-real-daft-error", observations["off"][1])
        self.assertIn("u2-real-daft-error", observations["observe"][1])

    def test_explicit_pth_bootstrap_hooks_fresh_driver_and_worker_interpreters(self):
        script = """
import json, os, site, sys
site.addsitedir(os.environ["UDFJIT_TEST_PURELIB"])
before = "daft" in sys.modules
import daft
from daft.dataframe.dataframe import DataFrame
from daft.udf.udf_v2 import Func
marker = "__python_udf_jit_u2_hook__"
print(json.dumps({
    "before": before,
    "func": bool(getattr(Func.__call__, marker, False)),
    "role": os.environ["UDFJIT_PROCESS_ROLE"],
    "select": bool(getattr(DataFrame.select, marker, False)),
    "where": bool(getattr(DataFrame.where, marker, False)),
    "with_columns": bool(getattr(DataFrame.with_columns, marker, False)),
}))
"""
        with tempfile.TemporaryDirectory() as directory:
            purelib = Path(directory)
            purelib.chmod(0o755)
            install_bootstrap(purelib)
            for role in ("driver", "worker"):
                for mode in ("off", "observe"):
                    with self.subTest(role=role, mode=mode):
                        environment = dict(
                            os.environ,
                            PYTHONPATH=os.pathsep.join(
                                path
                                for path in (
                                    str(Path(__file__).resolve().parents[2] / "src"),
                                    os.environ.get("PYTHONPATH", ""),
                                )
                                if path
                            ),
                            UDFJIT_MANIFEST_SHA256="d" * 64,
                            UDFJIT_MODE=mode,
                            UDFJIT_PROCESS_ROLE=role,
                            UDFJIT_TEST_PURELIB=str(purelib),
                        )
                        completed = subprocess.run(
                            [sys.executable, "-c", script],
                            check=False,
                            capture_output=True,
                            text=True,
                            env=environment,
                            timeout=30,
                        )

                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stderr,
                        )
                        observation = json.loads(completed.stdout)
                        self.assertFalse(observation["before"])
                        self.assertEqual(observation["role"], role)
                        expected = mode == "observe"
                        self.assertEqual(observation["func"], expected)
                        self.assertEqual(observation["where"], expected)
                        self.assertEqual(observation["select"], expected)
                        self.assertEqual(observation["with_columns"], expected)


if __name__ == "__main__":
    unittest.main()
