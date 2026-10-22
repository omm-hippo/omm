"""exo's loopback API, reduced to the shapes `omm cluster` needs.

Every field omm reads from exo is mapped here and nowhere else, so an exo
release that renames something breaks one file and its fixture tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from omm.cluster.errors import ClusterError


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


class ExoUnavailable(ClusterError):
    def __init__(self) -> None:
        super().__init__("exo is not running on this Mac")


_MIB = 1024 * 1024


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bytes(value) -> int:
    return _int(_dict(value).get("inBytes"))


def _instance_body(wrapper) -> dict:
    # Instances are tagged unions: {"MlxRingInstance": {...}} / {"MlxJacclInstance": {...}}.
    wrapper = _dict(wrapper)
    return _dict(next(iter(wrapper.values()), {})) if len(wrapper) == 1 else {}


def _sharding(body: dict) -> str:
    for shard in _dict(_dict(body.get("shardAssignments")).get("runnerToShard")).values():
        for tag in _dict(shard):
            return tag.removesuffix("ShardMetadata") or "unknown"
    return "unknown"


def parse_state(data: dict) -> ClusterState:
    data = _dict(data)
    memory = _dict(data.get("nodeMemory"))
    identities = _dict(data.get("nodeIdentities"))
    rdma = _dict(data.get("nodeRdmaCtl"))
    topology = _dict(data.get("topology"))
    node_ids = [n for n in topology.get("nodes", []) if isinstance(n, str)] if isinstance(topology.get("nodes"), list) else []
    for node_id in memory:
        if node_id not in node_ids:
            node_ids.append(node_id)
    nodes = []
    for node_id in node_ids:
        identity = _dict(identities.get(node_id))
        mem = _dict(memory.get(node_id))
        enabled = _dict(rdma.get(node_id)).get("enabled")
        name = identity.get("friendlyName")
        os_version = identity.get("osVersion")
        nodes.append(ClusterNode(
            node_id=node_id,
            name=name.replace(" ", " ") if isinstance(name, str) and name else node_id,
            memory_total_bytes=_bytes(mem.get("ramTotal")),
            memory_available_bytes=_bytes(mem.get("ramAvailable")),
            rdma="enabled" if enabled is True else "disabled" if enabled is False else "unknown",
            os_version=os_version if isinstance(os_version, str) else None,
        ))
    links = []
    for source, sinks in _dict(topology.get("connections")).items():
        for sink, edges in _dict(sinks).items():
            for edge in edges if isinstance(edges, list) else []:
                edge = _dict(edge)
                # RDMA edges name their rdma_enX ifaces; socket edges only carry a multiaddr.
                links.append(ClusterLink(source, sink, "rdma" if "sourceRdmaIface" in edge else "other"))
    instances = []
    for instance_id, wrapper in _dict(data.get("instances")).items():
        body = _instance_body(wrapper)
        assignments = _dict(body.get("shardAssignments"))
        model_id = assignments.get("modelId")
        instances.append(ClusterInstance(
            instance_id=str(body.get("instanceId") or instance_id),
            model_id=model_id if isinstance(model_id, str) else "",
            node_ids=tuple(_dict(assignments.get("nodeToRunner"))),
            sharding=_sharding(body),
        ))
    master = data.get("masterNodeId")
    return ClusterState(
        master_id=master if isinstance(master, str) else None,
        nodes=tuple(nodes),
        links=tuple(links),
        instances=tuple(instances),
    )


def parse_models(data: object) -> list[ExoModel]:
    rows = _dict(data).get("data")
    models = []
    for row in rows if isinstance(rows, list) else []:
        row = _dict(row)
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        size = row.get("storage_size_megabytes")
        models.append(ExoModel(model_id, _int(size) * _MIB if _int(size) > 0 else None))
    return models


def parse_previews(data: dict) -> list[Placement]:
    rows = _dict(data).get("previews")
    placements = []
    for row in rows if isinstance(rows, list) else []:
        row = _dict(row)
        deltas = {str(k): _int(v) for k, v in _dict(row.get("memory_delta_by_node")).items()}
        error = row.get("error")
        placements.append(Placement(
            model_id=str(row.get("model_id") or ""),
            sharding=str(row.get("sharding") or "unknown"),
            instance=_dict(row.get("instance")),
            memory_delta_by_node=deltas,
            error=None if error is None else str(error),
        ))
    return placements


def instance_ready(data: dict, model_id: str) -> bool:
    """True when an instance of `model_id` exists and every runner it uses is ready."""
    runners = _dict(_dict(data).get("runners"))
    for wrapper in _dict(_dict(data).get("instances")).values():
        assignments = _dict(_instance_body(wrapper).get("shardAssignments"))
        if assignments.get("modelId") != model_id:
            continue
        runner_ids = list(_dict(assignments.get("runnerToShard")))
        if runner_ids and all("RunnerReady" in _dict(runners.get(r)) for r in runner_ids):
            return True
    return False


class ExoClient:
    def __init__(self, base_url: str = "http://127.0.0.1:52415") -> None:
        from omm.engines.base import LoopbackJsonClient

        self._http = LoopbackJsonClient(base_url)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        from omm.engines.base import RuntimeAdapterError

        try:
            return self._http.request(method, path, **kwargs).data
        except RuntimeAdapterError as error:
            if error.reason == "server_unavailable":
                raise ExoUnavailable() from error
            raise ClusterError(f"exo request {method} {path.split('?', 1)[0]} failed: {error}") from error

    def _get(self, path: str, **kwargs) -> dict:
        return self._call("GET", path, **kwargs)

    def state(self) -> ClusterState:
        return parse_state(self._get("/state"))

    def models(self) -> list[ExoModel]:
        return parse_models(self._get("/models"))

    def previews(self, model_id: str) -> list[Placement]:
        from urllib.parse import quote

        return parse_previews(self._get(f"/instance/previews?model_id={quote(model_id, safe='')}", timeout=60))

    def create_instance(self, placement: Placement) -> str:
        data = self._call("POST", "/instance", payload={"instance": placement.instance}, timeout=60)
        return str(data.get("command_id") or "")

    def await_instance(self, model_id: str, timeout_seconds: float, *, sleep=None, clock=None) -> bool:
        import time

        sleep = sleep or time.sleep
        clock = clock or time.monotonic
        deadline = clock() + timeout_seconds
        while True:
            if instance_ready(self._get("/state"), model_id):
                return True
            if clock() >= deadline:
                return False
            sleep(2)

    def delete_instance(self, instance_id: str) -> None:
        from urllib.parse import quote

        self._call("DELETE", f"/instance/{quote(instance_id, safe='')}", allow_empty=True)
