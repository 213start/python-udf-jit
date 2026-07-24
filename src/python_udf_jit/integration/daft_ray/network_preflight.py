from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable, Mapping

from python_udf_jit.integration.daft_ray.environment import ContractViolation


DEFAULT_DATA_PLANE_SUBNET = "172.23.240.0/24"
DEFAULT_DASHBOARD_SUBNET = "172.23.241.0/24"


@dataclass(frozen=True)
class NetworkPreflightReport:
    requested_subnets: tuple[tuple[str, str], ...]
    host_routes: tuple[str, ...]
    docker_networks: tuple[tuple[str, str], ...]


def _parse_ipv4_network(value: str, *, source: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ContractViolation(f"invalid {source} IPv4 subnet {value!r}") from error
    if not isinstance(network, ipaddress.IPv4Network):
        raise ContractViolation(f"{source} must use an IPv4 subnet")
    return network


def validate_network_plan(
    *,
    requested_subnets: Mapping[str, str],
    host_routes: Iterable[str],
    docker_networks: Mapping[str, Iterable[str]],
) -> NetworkPreflightReport:
    if not requested_subnets:
        raise ContractViolation("at least one requested Docker subnet is required")

    requested = {
        name: _parse_ipv4_network(subnet, source=f"requested network {name}")
        for name, subnet in requested_subnets.items()
    }
    for (left_name, left), (right_name, right) in combinations(
        requested.items(), 2
    ):
        if left.overlaps(right):
            raise ContractViolation(
                f"requested Docker networks {left_name} ({left}) and "
                f"{right_name} ({right}) overlap each other"
            )

    parsed_routes = tuple(
        _parse_ipv4_network(route, source="host route") for route in host_routes
    )
    for name, subnet in requested.items():
        for route in parsed_routes:
            if subnet.overlaps(route):
                raise ContractViolation(
                    f"requested Docker network {name} ({subnet}) overlaps "
                    f"host route {route}"
                )

    parsed_docker_networks: list[tuple[str, ipaddress.IPv4Network]] = []
    for existing_name, subnets in docker_networks.items():
        for value in subnets:
            existing = _parse_ipv4_network(
                value, source=f"Docker network {existing_name}"
            )
            parsed_docker_networks.append((existing_name, existing))
            for requested_name, requested_subnet in requested.items():
                if requested_subnet.overlaps(existing):
                    raise ContractViolation(
                        f"requested Docker network {requested_name} "
                        f"({requested_subnet}) overlaps existing Docker network "
                        f"{existing_name} ({existing})"
                    )

    return NetworkPreflightReport(
        requested_subnets=tuple(
            sorted((name, str(subnet)) for name, subnet in requested.items())
        ),
        host_routes=tuple(sorted(str(route) for route in parsed_routes)),
        docker_networks=tuple(
            sorted((name, str(subnet)) for name, subnet in parsed_docker_networks)
        ),
    )


def _run_json(arguments: list[str], *, timeout: int = 30) -> object:
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractViolation(
            f"network preflight command failed: {arguments[0]}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "command returned no error detail"
        raise ContractViolation(
            f"network preflight command failed: {arguments[0]}: {detail}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ContractViolation(
            f"network preflight command returned invalid JSON: {arguments[0]}"
        ) from error


def collect_host_routes(*, executable: str = "ip") -> tuple[str, ...]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise ContractViolation(f"route executable {executable!r} not found")
    document = _run_json([resolved, "-j", "-4", "route", "show", "table", "main"])
    if not isinstance(document, list):
        raise ContractViolation("host route command did not return a JSON list")
    routes: set[str] = set()
    for item in document:
        if not isinstance(item, dict):
            raise ContractViolation("host route entry is not a JSON object")
        destination = item.get("dst")
        if destination in (None, "default"):
            continue
        routes.add(str(destination))
    return tuple(sorted(routes))


def collect_docker_networks(
    *, executable: str = "docker"
) -> dict[str, tuple[str, ...]]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise ContractViolation(f"Docker executable {executable!r} not found")
    network_ids = subprocess.run(
        [resolved, "network", "ls", "--format", "{{.ID}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if network_ids.returncode != 0:
        detail = network_ids.stderr.strip() or "docker network ls failed"
        raise ContractViolation(f"network preflight command failed: docker: {detail}")
    identifiers = tuple(line.strip() for line in network_ids.stdout.splitlines() if line.strip())
    if not identifiers:
        return {}
    document = _run_json([resolved, "network", "inspect", *identifiers])
    if not isinstance(document, list):
        raise ContractViolation("docker network inspect did not return a JSON list")

    networks: dict[str, tuple[str, ...]] = {}
    for item in document:
        if not isinstance(item, dict):
            raise ContractViolation("Docker network entry is not a JSON object")
        name = str(item.get("Name", ""))
        if not name:
            raise ContractViolation("Docker network entry is missing its name")
        ipam = item.get("IPAM")
        configurations = ipam.get("Config", []) if isinstance(ipam, dict) else []
        subnets = tuple(
            str(configuration["Subnet"])
            for configuration in configurations
            if isinstance(configuration, dict) and configuration.get("Subnet")
        )
        networks[name] = subnets
    return networks


def preflight_compose_networks(
    *,
    data_plane_subnet: str,
    dashboard_subnet: str,
) -> NetworkPreflightReport:
    return validate_network_plan(
        requested_subnets={
            "scalar-piercing": data_plane_subnet,
            "dashboard-loopback": dashboard_subnet,
        },
        host_routes=collect_host_routes(),
        docker_networks=collect_docker_networks(),
    )


def _write_report(path: Path, report: NetworkPreflightReport) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        json.dump(
            {"status": "ready", **asdict(report)},
            stream,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-plane-subnet",
        default=os.environ.get(
            "SCALAR_PIERCING_DATA_PLANE_SUBNET", DEFAULT_DATA_PLANE_SUBNET
        ),
    )
    parser.add_argument(
        "--dashboard-subnet",
        default=os.environ.get(
            "SCALAR_PIERCING_DASHBOARD_SUBNET", DEFAULT_DASHBOARD_SUBNET
        ),
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = preflight_compose_networks(
        data_plane_subnet=arguments.data_plane_subnet,
        dashboard_subnet=arguments.dashboard_subnet,
    )
    if arguments.output is not None:
        _write_report(arguments.output, report)
    print(json.dumps({"status": "ready", **asdict(report)}, sort_keys=True))


if __name__ == "__main__":
    main()
