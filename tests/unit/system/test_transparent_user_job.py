from __future__ import annotations

import ast
import json
import stat
import tempfile
import unittest
from pathlib import Path

from tests.system import transparent_user_job


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = ROOT / "tests/system/transparent_user_job.py"
DIAGNOSTIC_JOB_PATH = ROOT / "tests/e2e/live_job.py"


class TransparentUserJobContractTests(unittest.TestCase):
    def test_fixture_never_imports_or_calls_plugin_internals(self) -> None:
        source = FIXTURE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(FIXTURE_PATH))
        imported = []
        called = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.append(node.func.attr)

        self.assertFalse(
            [name for name in imported if name.startswith("python_udf_jit")]
        )
        self.assertFalse(
            {
                "_install_hooks",
                "install_default_daft_hooks",
                "install_daft_control_hooks",
                "compile",
            }
            & set(called)
        )
        self.assertNotIn("python_udf_jit", source)

    def test_ordered_digest_detects_reordering(self) -> None:
        rows = [
            {"row_id": 0, "measurement": 1.25, "result": 5.5},
            {"row_id": 1, "measurement": -2.5, "result": -2.0},
        ]

        original = transparent_user_job.ordered_result_sha256(rows)
        reordered = transparent_user_job.ordered_result_sha256(list(reversed(rows)))

        self.assertNotEqual(original, reordered)
        self.assertEqual(original, transparent_user_job.ordered_result_sha256(rows))

    def test_black_box_observes_framework_order_without_sorting(self) -> None:
        tree = ast.parse(
            FIXTURE_PATH.read_text(encoding="utf-8"),
            filename=str(FIXTURE_PATH),
        )
        observed_functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_with_column", "_with_columns", "_unsupported"}
        }

        self.assertEqual(
            set(observed_functions),
            {"_with_column", "_with_columns", "_unsupported"},
        )
        for name, function in observed_functions.items():
            with self.subTest(function=name):
                self.assertFalse(
                    any(
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sort"
                        for node in ast.walk(function)
                    ),
                    "ordered-result evidence must observe, not rewrite, row order",
                )

    def test_supported_diagnostic_path_does_not_install_hooks_before_use(self) -> None:
        tree = ast.parse(
            DIAGNOSTIC_JOB_PATH.read_text(encoding="utf-8"),
            filename=str(DIAGNOSTIC_JOB_PATH),
        )
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        function = functions["_diagnostic_job"]
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        udf_use_line = min(
            node.lineno for node in calls if node.func.id == "_input_frame"
        )
        install_lines = [
            node.lineno for node in calls if node.func.id == "_install_hooks"
        ]
        evidence_lines = [
            node.lineno
            for node in calls
            if node.func.id == "_runtime_events_since"
        ]

        self.assertTrue(
            install_lines or evidence_lines,
            "diagnostic probe must restore hooks afterward",
        )
        self.assertTrue(
            all(
                line > udf_use_line
                for line in (*install_lines, *evidence_lines)
            ),
            "supported path may only use hooks installed by process bootstrap",
        )

        if evidence_lines:
            helper_calls = [
                node
                for node in ast.walk(functions["_runtime_events_since"])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            ]
            self.assertTrue(
                any(node.func.id == "_uninstall_hooks" for node in helper_calls)
            )
            self.assertTrue(
                any(node.func.id == "_install_hooks" for node in helper_calls)
            )

        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (int, float))
        }
        self.assertEqual(constants["_FIXTURE_PARTITION_COUNT"], 32)
        self.assertEqual(constants["_SOURCES_PER_SCAN_TASK"], 8)
        self.assertEqual(constants["_MIN_CPU_PER_TASK"], 2.0)
        execution_config_calls = [
            node
            for node in ast.walk(functions["run_live_job"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_execution_config"
        ]
        self.assertEqual(len(execution_config_calls), 1)
        config_values = {
            keyword.arg: keyword.value
            for keyword in execution_config_calls[0].keywords
        }
        source_limit = config_values["max_sources_per_scan_task"]
        self.assertIsInstance(source_limit, ast.Name)
        self.assertEqual(source_limit.id, "_SOURCES_PER_SCAN_TASK")
        cpu_limit = config_values["min_cpu_per_task"]
        self.assertIsInstance(cpu_limit, ast.Name)
        self.assertEqual(cpu_limit.id, "_MIN_CPU_PER_TASK")

    def test_exception_observation_is_value_free_and_stable(self) -> None:
        error = RuntimeError(
            f"framework wrapper: {transparent_user_job.EXCEPTION_SENTINEL} "
            f"{transparent_user_job.USER_EXCEPTION_NAME}"
        )

        observation = transparent_user_job.exception_observation(error)

        self.assertEqual(observation["exception_type"], "builtins.RuntimeError")
        self.assertTrue(observation["user_exception_type_observed"])
        self.assertTrue(observation["message_sentinel_observed"])
        serialized = json.dumps(observation, sort_keys=True)
        self.assertNotIn(transparent_user_job.EXCEPTION_SENTINEL, serialized)
        self.assertNotIn(transparent_user_job.USER_EXCEPTION_NAME, serialized)
        self.assertNotIn("framework wrapper", serialized)

    def test_output_is_created_once_with_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "observation.json"
            document = {"schema_version": 1, "mode": "off"}

            transparent_user_job.write_output(output, document)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="ascii")), document)
            with self.assertRaises(FileExistsError):
                transparent_user_job.write_output(output, document)


if __name__ == "__main__":
    unittest.main()
