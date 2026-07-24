from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ContractViolation(ValueError):
    """Raised when the live topology differs from the fixed contract."""


@dataclass(frozen=True)
class TopologyNode:
    service: str
    role: str
    num_cpus: float


@dataclass(frozen=True)
class TopologyContract:
    nodes: tuple[TopologyNode, ...]
    execution_carrier_source_contract: dict[str, object]

    @property
    def head_services(self) -> tuple[str, ...]:
        return tuple(node.service for node in self.nodes if node.role == "head-driver")

    @property
    def worker_services(self) -> tuple[str, ...]:
        return tuple(node.service for node in self.nodes if node.role == "worker")

    def node(self, service: str) -> TopologyNode:
        for node in self.nodes:
            if node.service == service:
                return node
        raise ContractViolation(f"unknown topology service: {service}")


@dataclass(frozen=True)
class NodeObservation:
    node_id: str
    service: str
    alive: bool
    available_cpus: float


@dataclass(frozen=True)
class TopologySnapshot:
    head_node_id: str
    worker_node_ids: frozenset[str]
    readiness_targets: tuple[dict[str, object], ...]


def load_topology_contract(path: str | Path) -> TopologyContract:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes = tuple(
        TopologyNode(item["service"], item["role"], float(item["num_cpus"]))
        for item in document["nodes"]
    )
    contract = TopologyContract(
        nodes=nodes,
        execution_carrier_source_contract=dict(
            document["execution_carrier_source_contract"]
        ),
    )
    if len(nodes) != 3 or len(contract.head_services) != 1 or len(contract.worker_services) != 2:
        raise ContractViolation("topology contract requires exactly one Head and two Workers")
    source = contract.execution_carrier_source_contract
    if not (
        source.get("source") == "daft/runners/flotilla.py::start_ray_workers"
        and source.get("eligible_node_resource") == "Resources.CPU > 0"
        and source.get("actor_class") == "RaySwordfishActor"
        and source.get("scheduling_strategy") == "NodeAffinitySchedulingStrategy"
        and source.get("soft") is False
    ):
        raise ContractViolation("Daft v0.7.2 execution carrier source contract drift")
    return contract


def validate_three_node_topology(
    contract: TopologyContract,
    observations: Iterable[NodeObservation],
) -> TopologySnapshot:
    alive = tuple(observation for observation in observations if observation.alive)
    if len(alive) != 3:
        raise ContractViolation("live Ray cluster must contain exactly three Alive nodes")
    by_service = {observation.service: observation for observation in alive}
    if len(by_service) != 3 or set(by_service) != {node.service for node in contract.nodes}:
        raise ContractViolation("live Ray roles must exactly match the topology contract")
    if len({observation.node_id for observation in alive}) != 3:
        raise ContractViolation("live Ray nodes require three unique Node IDs")

    head = by_service[contract.head_services[0]]
    if head.available_cpus != 0:
        raise ContractViolation("Head/Driver must report zero logical CPU")
    workers = tuple(by_service[service] for service in contract.worker_services)
    if any(worker.available_cpus <= 0 for worker in workers):
        raise ContractViolation("each Worker must report positive logical CPU")
    readiness = tuple(
        {"node_id": worker.node_id, "soft": False} for worker in workers
    )
    return TopologySnapshot(
        head_node_id=head.node_id,
        worker_node_ids=frozenset(worker.node_id for worker in workers),
        readiness_targets=readiness,
    )


def assert_data_plane_isolation(
    *,
    head_node_id: str,
    worker_node_ids: set[str] | frozenset[str],
    event_node_ids: Iterable[str],
) -> None:
    for node_id in event_node_ids:
        if node_id == head_node_id:
            raise ContractViolation("data-plane execution occurred on Head/Driver")
        if node_id not in worker_node_ids:
            raise ContractViolation(f"data-plane event came from unknown node {node_id!r}")
