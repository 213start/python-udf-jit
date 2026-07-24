from __future__ import annotations

import ast
import copy
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from python_udf_jit.diagnostics.environment_evidence import (
    seal_environment_proof,
    validate_cleanup_evidence,
)
from tests.system.capture_host_state import _canonical_routes
from tests.system.run_blue98_acceptance import (
    AcceptanceRunError,
    RunLayout,
    _compose_project,
    _down,
    _install_trusted_root_environment,
    _project_ids,
    _restart_worker_and_capture,
    _scrub_failure_payloads,
)
from tests.system.verify_cleanup import build_cleanup_proof


def _state() -> dict[str, object]:
    return {
        "routes_sha256": "a" * 64,
        "firewall_sha256": "b" * 64,
        "firewalld_runtime_sha256": "c" * 64,
        "firewalld_permanent_sha256": "d" * 64,
        "firewall_backend": "nftables-stateless",
        "firewalld_state": "running",
    }


def _bridge() -> dict[str, object]:
    network_id = "f" * 64
    return {
        "action": "runtime-trusted",
        "network_id": network_id,
        "bridge_interface": f"br-{network_id[:12]}",
        "zone": "trusted",
        "scope": "runtime",
        "connectivity_before": {
            "ray-worker-1": False,
            "ray-worker-2": False,
        },
        "connectivity_after": {
            "ray-worker-1": True,
            "ray-worker-2": True,
        },
        "binding_added": True,
        "binding_removed": True,
        "bridge_interface_exists_after_cleanup": False,
    }


class CleanupProbeTests(unittest.TestCase):
    def test_route_hash_input_is_canonical_json(self) -> None:
        left = _canonical_routes(
            b'[{"dst":"default","gateway":"1.2.3.4","metric":100}]'
        )
        right = _canonical_routes(
            b'[{"metric":100,"gateway":"1.2.3.4","dst":"default"}]'
        )
        self.assertEqual(left, right)

    def test_real_resource_identifiers_and_equal_host_state_seal_proof(self) -> None:
        class ProjectCommands:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, arguments: list[str]) -> str:
                self.calls.append(arguments)
                if arguments[1] == "ps":
                    return f"{'c' * 64}\n{'d' * 64}\n{'e' * 64}\n"
                return f"{'f' * 64}\n{'1' * 64}\n"

        commands = ProjectCommands()
        self.assertEqual(
            _project_ids(
                commands,
                kind="container",
                project="u13-project",
                run_id="u13-run",
            ),
            ["c" * 64, "d" * 64, "e" * 64],
        )
        self.assertEqual(
            _project_ids(
                commands,
                kind="network",
                project="u13-project",
                run_id="u13-run",
            ),
            ["1" * 64, "f" * 64],
        )
        self.assertTrue(
            all("--no-trunc" in arguments for arguments in commands.calls)
        )
        self.assertTrue(
            all(
                "label=org.python-udf-jit.run-id=u13-run" in arguments
                for arguments in commands.calls
            )
        )

        state = _state()
        proof = build_cleanup_proof(
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            before=state,
            after=state,
            removed_container_ids=["c" * 64, "d" * 64, "e" * 64],
            removed_network_ids=["f" * 64, "1" * 64],
            remaining_project_containers=[],
            remaining_project_networks=[],
            dashboard_port_open=False,
            token_exists=False,
            bridge_accommodation=_bridge(),
        )

        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "pass",
        )

        short_document = copy.deepcopy(
            {
                key: value
                for key, value in proof.items()
                if key != "proof_sha256"
            }
        )
        short_document["cleanup"]["removed_network_ids"] = [
            identifier[:12]
            for identifier in proof["cleanup"]["removed_network_ids"]
        ]
        short_ids = seal_environment_proof(short_document)
        self.assertEqual(
            validate_cleanup_evidence(
                short_ids,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )

    def test_compose_project_is_unique_per_cluster_epoch(self) -> None:
        first = _compose_project("epoch-" + "a" * 24)
        second = _compose_project("epoch-" + "b" * 24)

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("u13-"))
        self.assertTrue(second.startswith("u13-"))

    def test_down_attempts_network_cleanup_after_container_removal_failure(
        self,
    ) -> None:
        container_id = "c" * 64
        network_id = "f" * 64

        class FailingCommands:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.container_queries = 0
                self.network_queries = 0

            def run(
                self,
                arguments: list[str],
                **_: object,
            ) -> str:
                self.calls.append(arguments)
                if arguments[:2] == ["compose", "down"]:
                    raise AcceptanceRunError("compose_down_failed")
                if arguments[:2] == ["docker", "ps"]:
                    self.container_queries += 1
                    return f"{container_id}\n"
                if arguments[:3] == ["docker", "network", "ls"]:
                    self.network_queries += 1
                    return f"{network_id}\n" if self.network_queries == 1 else ""
                if arguments[:3] == ["docker", "rm", "-f"]:
                    raise AcceptanceRunError("container_removal_failed")
                if arguments[:3] == ["docker", "network", "rm"]:
                    return ""
                raise AssertionError(arguments)

        commands = FailingCommands()
        with self.assertRaisesRegex(
            AcceptanceRunError,
            "container_removal:AcceptanceRunError",
        ):
            _down(
                commands,
                compose=["compose"],
                environment={},
                project="u13-project",
                run_id="u13-run",
                log_path=None,
            )

        self.assertIn(
            ["docker", "network", "rm", network_id],
            commands.calls,
        )
        self.assertEqual(commands.network_queries, 2)

    def test_successful_compose_down_still_verifies_no_resources_remain(
        self,
    ) -> None:
        class LeakingCommands:
            def run(
                self,
                arguments: list[str],
                **_: object,
            ) -> str:
                if arguments[:2] == ["compose", "down"]:
                    return ""
                if arguments[:2] == ["docker", "ps"]:
                    return f"{'c' * 64}\n"
                if arguments[:3] == ["docker", "network", "ls"]:
                    return ""
                raise AssertionError(arguments)

        with self.assertRaisesRegex(
            AcceptanceRunError,
            "container_resources_remain",
        ):
            _down(
                LeakingCommands(),
                compose=["compose"],
                environment={},
                project="u13-project",
                run_id="u13-run",
                log_path=None,
            )

    def test_failure_scrub_removes_all_payloads_but_preserves_run_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = RunLayout.create(Path(directory) / "u13-run")
            for root in (
                layout.evidence,
                layout.logs,
                layout.private,
                layout.work,
            ):
                nested = root / "nested"
                nested.mkdir()
                (nested / "payload").write_bytes(b"sensitive")

            _scrub_failure_payloads(layout)

            self.assertTrue(layout.root.is_dir())
            self.assertFalse(
                any(
                    path.is_file()
                    for root in (
                        layout.evidence,
                        layout.logs,
                        layout.private,
                        layout.work,
                    )
                    for path in root.rglob("*")
                )
            )

    def test_restart_capture_waits_for_cluster_before_snapshot(self) -> None:
        calls: list[str] = []

        class RecordingCommands:
            def log(
                self,
                arguments: list[str],
                path: Path,
                **kwargs: object,
            ) -> None:
                calls.append("restart")
                self.arguments = arguments
                self.path = path
                self.kwargs = kwargs

        commands = RecordingCommands()

        def await_cluster(*_args: object, **_kwargs: object) -> None:
            calls.append("await")

        def capture_snapshot(**_kwargs: object) -> dict[str, object]:
            calls.append("capture")
            return {"status": "captured"}

        with (
            mock.patch(
                "tests.system.run_blue98_acceptance._await_cluster",
                side_effect=await_cluster,
            ),
            mock.patch(
                "tests.system.run_blue98_acceptance._capture_snapshot",
                side_effect=capture_snapshot,
            ),
        ):
            result = _restart_worker_and_capture(
                commands,
                worker_container="worker-2",
                head_container="head",
                run_id="u13-run",
                cluster_epoch="u13-epoch",
                manifest_sha256="a" * 64,
                containers={
                    "ray-head-driver": "head",
                    "ray-worker-1": "worker-1",
                    "ray-worker-2": "worker-2",
                },
                output=Path("/tmp/after.json"),
                log_path=Path("/tmp/restart.log"),
            )

        self.assertEqual(calls, ["restart", "await", "capture"])
        self.assertEqual(result, {"status": "captured"})
        self.assertEqual(
            commands.arguments,
            ["docker", "restart", "--time", "10", "worker-2"],
        )

    def test_restart_baseline_is_captured_after_live_suite(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[2]
            / "system"
            / "run_blue98_acceptance.py"
        )
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        live_receipt_line = next(
            node.lineno
            for node in ast.walk(run_function)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "live_proof"
                for target in node.targets
            )
        )
        baseline_line = next(
            node.lineno
            for node in ast.walk(run_function)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "e2e_snapshot_path"
                for target in node.targets
            )
        )
        restart_line = next(
            node.lineno
            for node in ast.walk(run_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_restart_worker_and_capture"
        )

        self.assertLess(live_receipt_line, baseline_line)
        self.assertLess(baseline_line, restart_line)

    def test_root_environment_discards_inherited_execution_controls(
        self,
    ) -> None:
        resolved: list[tuple[str, str]] = []

        class TrustedDirectory:
            def __init__(self, value: str) -> None:
                self.value = value

            def is_dir(self) -> bool:
                return True

            def stat(self) -> os.stat_result:
                return mock.Mock(
                    st_uid=0,
                    st_mode=stat.S_IFDIR | 0o755,
                )

        def resolver(name: str, search_path: str) -> str:
            resolved.append((name, search_path))
            return f"/usr/bin/{name}"

        with (
            mock.patch(
                "tests.system.run_blue98_acceptance._TRUSTED_TOOL_DIRECTORIES",
                ("/usr/bin", "/bin"),
            ),
            mock.patch(
                "tests.system.run_blue98_acceptance.Path",
                TrustedDirectory,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "PATH": "/tmp/attacker",
                    "HTTPS_PROXY": "http://proxy.invalid",
                    "LD_PRELOAD": "/tmp/inject.so",
                },
                clear=True,
            ),
        ):
            tools = _install_trusted_root_environment(resolver)

            self.assertEqual(
                dict(os.environ),
                {
                    "HOME": "/root",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
            )

        self.assertEqual(set(tools), {name for name, _ in resolved})
        self.assertTrue(
            all(search_path == "/usr/bin:/bin" for _, search_path in resolved)
        )

    def test_any_remaining_resource_is_preserved_as_failure_evidence(self) -> None:
        state = _state()
        proof = build_cleanup_proof(
            run_id="u13-run",
            cluster_epoch="u13-epoch",
            before=state,
            after=state,
            removed_container_ids=["c" * 64, "d" * 64, "e" * 64],
            removed_network_ids=["f" * 64, "1" * 64],
            remaining_project_containers=["still-running"],
            remaining_project_networks=[],
            dashboard_port_open=False,
            token_exists=False,
            bridge_accommodation=_bridge(),
        )

        self.assertEqual(
            validate_cleanup_evidence(
                proof,
                run_id="u13-run",
                cluster_epoch="u13-epoch",
            ),
            "fail",
        )


if __name__ == "__main__":
    unittest.main()
