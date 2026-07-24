from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol


_NETWORK_ID = re.compile(r"^[0-9a-f]{64}$")
_INTERFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


class CommandRunner(Protocol):
    def run(self, arguments: Iterable[str], **kwargs: object) -> str: ...


@dataclass(frozen=True)
class DockerBridge:
    logical_network: str
    network_id: str
    interface: str


@dataclass(frozen=True)
class RuntimeZoneBinding:
    bridge: DockerBridge
    zone: str = "trusted"


def _one_network_document(payload: str) -> Mapping[str, object]:
    document = json.loads(payload)
    if (
        not isinstance(document, list)
        or len(document) != 1
        or not isinstance(document[0], Mapping)
    ):
        raise RuntimeError("one Docker network inspection is required")
    return document[0]


def resolve_project_bridge(
    commands: CommandRunner,
    *,
    project: str,
    logical_network: str,
) -> DockerBridge:
    identifiers = [
        line.strip()
        for line in commands.run(
            [
                "docker",
                "network",
                "ls",
                "--no-trunc",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.network={logical_network}",
            ]
        ).splitlines()
        if line.strip()
    ]
    if len(identifiers) != 1 or _NETWORK_ID.fullmatch(identifiers[0]) is None:
        raise RuntimeError(
            f"one full project network id is required: {identifiers!r}"
        )
    network_id = identifiers[0]
    network = _one_network_document(
        commands.run(["docker", "network", "inspect", network_id])
    )
    labels = network.get("Labels")
    options = network.get("Options")
    if (
        network.get("Id") != network_id
        or network.get("Driver") != "bridge"
        or network.get("Scope") != "local"
        or network.get("Internal") is not True
        or not isinstance(labels, Mapping)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.network") != logical_network
        or not isinstance(options, Mapping)
    ):
        raise RuntimeError("project data-plane network is not an internal bridge")
    configured_name = options.get("com.docker.network.bridge.name")
    interface = (
        str(configured_name)
        if configured_name
        else f"br-{network_id[:12]}"
    )
    if _INTERFACE.fullmatch(interface) is None:
        raise RuntimeError(f"unsafe Docker bridge interface: {interface!r}")
    links = json.loads(
        commands.run(["ip", "-j", "link", "show", "dev", interface])
    )
    if (
        not isinstance(links, list)
        or len(links) != 1
        or not isinstance(links[0], Mapping)
        or links[0].get("ifname") != interface
    ):
        raise RuntimeError(f"Docker bridge interface not found: {interface}")
    return DockerBridge(
        logical_network=logical_network,
        network_id=network_id,
        interface=interface,
    )


def _trusted_interfaces(commands: CommandRunner) -> set[str]:
    return set(
        commands.run(
            ["firewall-cmd", "--zone=trusted", "--list-interfaces"]
        ).split()
    )


def bind_runtime_trusted(
    commands: CommandRunner,
    bridge: DockerBridge,
) -> RuntimeZoneBinding:
    if commands.run(["firewall-cmd", "--state"]).strip() != "running":
        raise RuntimeError("firewalld must be running")
    if bridge.interface in _trusted_interfaces(commands):
        raise RuntimeError(
            f"bridge already has a trusted runtime binding: {bridge.interface}"
        )
    add_arguments = [
        "firewall-cmd",
        "--zone=trusted",
        f"--add-interface={bridge.interface}",
    ]
    remove_arguments = [
        "firewall-cmd",
        "--zone=trusted",
        f"--remove-interface={bridge.interface}",
    ]
    try:
        commands.run(add_arguments)
        if bridge.interface not in _trusted_interfaces(commands):
            raise RuntimeError(
                "trusted runtime binding was not installed: "
                f"{bridge.interface}"
            )
    except BaseException as error:
        rollback_error: BaseException | None = None
        try:
            commands.run(remove_arguments)
        except BaseException as caught:
            try:
                if bridge.interface in _trusted_interfaces(commands):
                    rollback_error = caught
            except BaseException as verification_error:
                rollback_error = RuntimeError(
                    f"{caught}; verification={verification_error}"
                )
        else:
            try:
                if bridge.interface in _trusted_interfaces(commands):
                    rollback_error = RuntimeError(
                        "bridge remains trusted after rollback"
                    )
            except BaseException as verification_error:
                rollback_error = verification_error
        if rollback_error is not None:
            raise RuntimeError(
                "firewalld runtime binding failed and rollback could not "
                f"be verified: add={error}; rollback={rollback_error}"
            ) from error
        raise
    return RuntimeZoneBinding(bridge=bridge)


def unbind_runtime_trusted(
    commands: CommandRunner,
    binding: RuntimeZoneBinding,
) -> None:
    interface = binding.bridge.interface
    if interface not in _trusted_interfaces(commands):
        raise RuntimeError(
            f"trusted runtime binding disappeared before cleanup: {interface}"
        )
    commands.run(
        [
            "firewall-cmd",
            f"--zone={binding.zone}",
            f"--remove-interface={interface}",
        ]
    )
    if interface in _trusted_interfaces(commands):
        raise RuntimeError(
            f"trusted runtime binding remains after cleanup: {interface}"
        )
