from __future__ import annotations

import json
import unittest
from collections.abc import Iterable

from tests.system.firewalld_bridge import (
    bind_runtime_trusted,
    resolve_project_bridge,
    unbind_runtime_trusted,
)


class _Commands:
    def __init__(
        self,
        *,
        internal: bool = True,
        hide_binding_after_add: bool = False,
    ) -> None:
        self.network_id = "a" * 64
        self.interface = f"br-{self.network_id[:12]}"
        self.internal = internal
        self.hide_binding_after_add = hide_binding_after_add
        self.hide_next_binding_query = False
        self.trusted: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Iterable[str],
        **kwargs: object,
    ) -> str:
        del kwargs
        argv = tuple(arguments)
        self.calls.append(argv)
        if argv[:3] == ("docker", "network", "ls"):
            return f"{self.network_id}\n"
        if argv[:3] == ("docker", "network", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": self.network_id,
                        "Driver": "bridge",
                        "Scope": "local",
                        "Internal": self.internal,
                        "Labels": {
                            "com.docker.compose.project": "u13project",
                            "com.docker.compose.network": "scalar-piercing",
                        },
                        "Options": {},
                    }
                ]
            )
        if argv[:4] == ("ip", "-j", "link", "show"):
            return json.dumps([{"ifname": self.interface}])
        if argv == ("firewall-cmd", "--state"):
            return "running\n"
        if argv == (
            "firewall-cmd",
            "--zone=trusted",
            "--list-interfaces",
        ):
            if self.hide_next_binding_query:
                self.hide_next_binding_query = False
                return ""
            return " ".join(sorted(self.trusted))
        if argv == (
            "firewall-cmd",
            "--zone=trusted",
            f"--add-interface={self.interface}",
        ):
            self.trusted.add(self.interface)
            self.hide_next_binding_query = self.hide_binding_after_add
            return "success\n"
        if argv == (
            "firewall-cmd",
            "--zone=trusted",
            f"--remove-interface={self.interface}",
        ):
            self.trusted.remove(self.interface)
            return "success\n"
        raise AssertionError(f"unexpected command: {argv!r}")


class FirewalldBridgeTests(unittest.TestCase):
    def test_resolves_exact_internal_project_bridge(self) -> None:
        commands = _Commands()

        bridge = resolve_project_bridge(
            commands,
            project="u13project",
            logical_network="scalar-piercing",
        )

        self.assertEqual(bridge.network_id, commands.network_id)
        self.assertEqual(bridge.interface, commands.interface)

    def test_rejects_non_internal_data_plane(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "internal bridge"):
            resolve_project_bridge(
                _Commands(internal=False),
                project="u13project",
                logical_network="scalar-piercing",
            )

    def test_runtime_binding_is_exact_and_reversible(self) -> None:
        commands = _Commands()
        bridge = resolve_project_bridge(
            commands,
            project="u13project",
            logical_network="scalar-piercing",
        )

        binding = bind_runtime_trusted(commands, bridge)
        self.assertEqual(commands.trusted, {commands.interface})
        unbind_runtime_trusted(commands, binding)

        self.assertEqual(commands.trusted, set())
        self.assertFalse(
            any(
                "--permanent" in argument
                for call in commands.calls
                for argument in call
            )
        )

        rollback = _Commands(hide_binding_after_add=True)
        rollback_bridge = resolve_project_bridge(
            rollback,
            project="u13project",
            logical_network="scalar-piercing",
        )
        with self.assertRaisesRegex(RuntimeError, "was not installed"):
            bind_runtime_trusted(rollback, rollback_bridge)
        self.assertEqual(rollback.trusted, set())


if __name__ == "__main__":
    unittest.main()
