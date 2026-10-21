"""exo's loopback API, reduced to the shapes `omm cluster` needs.

Every field omm reads from exo is mapped here and nowhere else, so an exo
release that renames something breaks one file and its fixture tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ClusterNode:
    node_id: str
    name: str
    memory_total_bytes: int
    memory_available_bytes: int
    rdma: Literal["enabled", "disabled", "unknown"]
    os_version: str | None


@dataclass(frozen=True)
class ClusterLink:
    source_id: str
    sink_id: str
    kind: Literal["thunderbolt", "rdma", "other"]


@dataclass(frozen=True)
class ClusterInstance:
    instance_id: str
    model_id: str
    node_ids: tuple[str, ...]
    sharding: str


@dataclass(frozen=True)
class ClusterState:
    master_id: str | None
    nodes: tuple[ClusterNode, ...]
    links: tuple[ClusterLink, ...]
    instances: tuple[ClusterInstance, ...]


@dataclass(frozen=True)
class ExoModel:
    model_id: str
    size_bytes: int | None


@dataclass(frozen=True)
class Placement:
    model_id: str
    sharding: str
    instance: dict = field(hash=False, compare=True)
    memory_delta_by_node: dict = field(hash=False, compare=True)
    error: str | None = None
