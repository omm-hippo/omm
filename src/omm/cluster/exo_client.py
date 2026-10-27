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
    # None until exo's first memory report for the node (right after a restart
    # a node is listed in the topology before its memory arrives).
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    rdma: Literal["enabled", "disabled", "unknown"]
    os_version: str | None
    thunderbolt_bridge: bool | None = None
    os_build: str | None = None


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
    # exo's own loop detection (`thunderboltBridgeCycles`): node ids whose
    # Thunderbolt Bridges form a cycle. exo lists every bidirectional link as
    # a 2-node cycle too; only 3 or more nodes mean a real loop.
    bridge_cycles: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ExoModel:
    model_id: str
    size_bytes: int | None
    # The raw /models row (family, quantization, context_length, ...) for `omm cluster info`.
    card: dict = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class NodeDownload:
    """One Mac's copy of one model, from `/state` `downloads`.

    exo lists a Pending entry for every catalog model on every Mac, so a model
    is on a Mac only when its entry is Completed. A Pending entry carries the
    full-range shardMetadata that `/download/start` takes (rig, 2026-10-26).
    """

    node_id: str
    model_id: str
    kind: Literal["Pending", "Ongoing", "Completed", "Failed"]
    done_bytes: int
    total_bytes: int
    shard_metadata: dict = field(hash=False, compare=False)
    error: str | None = None


@dataclass(frozen=True)
class SearchHit:
    model_id: str
    downloads: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Placement:
    model_id: str
    sharding: str
    instance: dict = field(hash=False, compare=True)
    memory_delta_by_node: dict = field(hash=False, compare=True)
    error: str | None = None
    # The backend exo tried ("MlxRing" / "MlxJaccl"); the only one an error row names.
    instance_meta: str = ""


@dataclass(frozen=True)
class LoadProgress:
    """Where a model that is being loaded stands, read from exo's `/state`."""

    phase: Literal["downloading", "loading"]
    # The Mac furthest behind, because every Mac must finish before the model
    # can serve. total_bytes is 0 until exo has sized the download.
    downloaded_bytes: int = 0
    total_bytes: int = 0
    # Summed over all Macs; only used to tell whether anything moved.
    transferred_bytes: int = 0
    failed: str | None = None
    # The Mac whose download failed (its name, else its node id).
    failed_on: str | None = None


class ExoUnavailable(ClusterError):
    def __init__(self) -> None:
        super().__init__("exo is not running on this Mac")


_MIB = 1024 * 1024
# What exo puts in every preview row of a model that does not fit the Macs'
# free memory (exo 1.0.71, 3 Mac minis, 2026-10-26).
NO_MEMORY = "No cycles found with sufficient memory"


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bytes(value) -> int | None:
    value = _dict(value).get("inBytes")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    bridges = _dict(data.get("nodeThunderboltBridge"))
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
        os_build = identity.get("osBuildVersion")
        nodes.append(ClusterNode(
            node_id=node_id,
            name=name.replace(" ", " ") if isinstance(name, str) and name else node_id,
            memory_total_bytes=_bytes(mem.get("ramTotal")),
            memory_available_bytes=_bytes(mem.get("ramAvailable")),
            rdma="enabled" if enabled is True else "disabled" if enabled is False else "unknown",
            os_version=os_version if isinstance(os_version, str) else None,
            thunderbolt_bridge=bridge if isinstance(bridge := _dict(bridges.get(node_id)).get("enabled"), bool) else None,
            os_build=os_build if isinstance(os_build, str) and os_build else None,
        ))
    links = []
    for source, sinks in _dict(topology.get("connections")).items():
        for sink, edges in _dict(sinks).items():
            for edge in edges if isinstance(edges, list) else []:
                edge = _dict(edge)
                # RDMA edges name their rdma_enX ifaces; socket edges only carry a multiaddr.
                links.append(ClusterLink(source, sink, "rdma" if "sourceRdmaIface" in edge else "other"))
    cycles = []
    for row in data.get("thunderboltBridgeCycles") or []:
        if isinstance(row, list) and row and all(isinstance(n, str) for n in row):
            cycles.append(tuple(row))
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
        bridge_cycles=tuple(cycles),
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
        models.append(ExoModel(model_id, _int(size) * _MIB if _int(size) > 0 else None, row))
    return models


def parse_search(data: object) -> list[SearchHit]:
    hits = []
    for row in data if isinstance(data, list) else []:
        row = _dict(row)
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        tags = row.get("tags")
        hits.append(SearchHit(model_id, _int(row.get("downloads")),
                              tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else ()))
    return hits


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
            instance_meta=str(row.get("instance_meta") or ""),
        ))
    return placements


def node_ipv4s(data: dict) -> dict[str, list[str]]:
    """Each node's IPv4 addresses from `/state` `nodeNetwork`, loopback left out."""
    addresses = {}
    for node_id, network in _dict(data.get("nodeNetwork")).items():
        found = []
        for interface in _dict(network).get("interfaces") or []:
            address = _dict(interface).get("ipAddress")
            if isinstance(address, str) and address.count(".") == 3 and not address.startswith("127."):
                found.append(address)
        addresses[str(node_id)] = found
    return addresses


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


def _download_entry(wrapper, model_id: str) -> tuple[str, dict] | None:
    # Downloads are tagged unions like instances: {"DownloadOngoing": {...}}.
    wrapper = _dict(wrapper)
    if len(wrapper) != 1:
        return None
    kind, body = next(iter(wrapper.items()))
    body = _dict(body)
    shard = _dict(next(iter(_dict(body.get("shardMetadata")).values()), {}))
    if _dict(shard.get("modelCard")).get("modelId") != model_id:
        return None
    return kind, body


def parse_downloads(data: dict) -> list[NodeDownload]:
    rows = []
    for node_id, items in _dict(_dict(data).get("downloads")).items():
        for wrapper in items if isinstance(items, list) else []:
            wrapper = _dict(wrapper)
            if len(wrapper) != 1:
                continue
            tag, body = next(iter(wrapper.items()))
            kind = str(tag).removeprefix("Download")
            if kind not in ("Pending", "Ongoing", "Completed", "Failed"):
                continue
            body = _dict(body)
            metadata = _dict(body.get("shardMetadata"))
            model_id = _dict(_dict(next(iter(metadata.values()), {})).get("modelCard")).get("modelId")
            if not isinstance(model_id, str) or not model_id:
                continue
            sizes = _dict(body.get("downloadProgress")) if kind == "Ongoing" else body
            total = _bytes(sizes.get("total")) or 0
            done = total if kind == "Completed" else _bytes(sizes.get("downloaded")) or 0
            error = body.get("errorMessage") if kind == "Failed" else None
            rows.append(NodeDownload(str(node_id), model_id, kind, done, total, metadata,
                                     str(error or "download failed") if kind == "Failed" else None))
    return rows


def load_progress(data: dict, model_id: str) -> LoadProgress:
    """Download and load progress of `model_id` on the Macs its instance uses (every Mac before one exists).

    exo keeps one download entry per node and model. Pending, Ongoing and
    Failed entries mean a Mac still has files to fetch; once every entry is
    Completed (or exo lists none) the Macs are loading weights into memory.
    A Mac outside the instance keeps its Pending entry and never fetches it
    (Tensor on 2 of 3 Macs, 2026-10-27).
    """
    nodes = {node for instance in parse_state(_dict(data)).instances if instance.model_id == model_id
             for node in instance.node_ids} or None
    unfinished: list[tuple[int, int]] = []
    transferred = 0
    failure: str | None = None
    failed_on: str | None = None
    identities = _dict(_dict(data).get("nodeIdentities"))
    for node_id, items in _dict(_dict(data).get("downloads")).items():
        if nodes is not None and node_id not in nodes:
            continue
        for wrapper in items if isinstance(items, list) else []:
            entry = _download_entry(wrapper, model_id)
            if entry is None:
                continue
            kind, body = entry
            if kind == "DownloadCompleted":
                transferred += _bytes(body.get("total")) or 0
                continue
            if kind == "DownloadFailed":
                if failure is None:
                    failure = str(body.get("errorMessage") or "download failed")
                    name = _dict(identities.get(node_id)).get("friendlyName")
                    failed_on = name.replace("\u00a0", " ") if isinstance(name, str) and name else str(node_id)
                unfinished.append((0, 0))
                continue
            if kind == "DownloadOngoing":
                body = _dict(body.get("downloadProgress"))
            elif kind != "DownloadPending":
                continue
            done, total = _bytes(body.get("downloaded")) or 0, _bytes(body.get("total")) or 0
            transferred += done
            unfinished.append((done, total))
    if not unfinished:
        return LoadProgress("loading", transferred_bytes=transferred)
    sized = [(done, total) for done, total in unfinished if total > 0]
    done, total = min(sized, key=lambda pair: pair[0] / pair[1]) if sized else (0, 0)
    return LoadProgress("downloading", done, total, transferred, failure, failed_on)


class ExoClient:
    def __init__(self, base_url: str = "http://127.0.0.1:52415") -> None:
        from omm.engines.base import LoopbackJsonClient

        self._base_url = base_url.rstrip("/")
        self._http = LoopbackJsonClient(base_url)

    @property
    def base_url(self) -> str:
        return self._base_url

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

    def raw_state(self, timeout: float = 10) -> dict:
        # For callers that need both parse_state and the load helpers
        # (instance_ready, load_progress) from one consistent snapshot.
        return self._get("/state", timeout=timeout)

    def state(self) -> ClusterState:
        return parse_state(self.raw_state())

    def models(self) -> list[ExoModel]:
        return parse_models(self._get("/models"))

    def previews(self, model_id: str) -> list[Placement]:
        from urllib.parse import quote

        return parse_previews(self._get(f"/instance/previews?model_id={quote(model_id, safe='')}", timeout=60))

    def create_instance(self, placement: Placement) -> str:
        data = self._call("POST", "/instance", payload={"instance": placement.instance}, timeout=60)
        return str(data.get("command_id") or "")

    def await_instance(self, model_id: str, stall_seconds: float, *, on_progress=None, sleep=None, clock=None) -> bool:
        """Wait until the model serves; False once nothing has moved for `stall_seconds`.

        A first load downloads the whole model on every Mac (hours for a big
        one), so a fixed deadline cannot fit it. Progress is any change in the
        bytes fetched or in a runner's state.
        """
        import time

        sleep = sleep or time.sleep
        clock = clock or time.monotonic
        last_change = clock()
        signature = None
        while True:
            data = self._get("/state")
            if instance_ready(data, model_id):
                return True
            progress = load_progress(data, model_id)
            # Until exo places the instance, every Mac's Pending entry would read as 0% downloaded.
            placed = any(instance.model_id == model_id for instance in parse_state(data).instances)
            if on_progress is not None and (placed or progress.failed):
                on_progress(progress)
            if progress.failed:
                # exo does not try a failed download again until it restarts.
                return False
            runners = tuple(sorted((str(k), tuple(_dict(v))) for k, v in _dict(_dict(data).get("runners")).items()))
            now = clock()
            if (progress.transferred_bytes, runners) != signature:
                signature = (progress.transferred_bytes, runners)
                last_change = now
            elif now - last_change >= stall_seconds:
                return False
            sleep(2)

    def start_download(self, node_id: str, shard_metadata: dict) -> None:
        self._call("POST", "/download/start",
                   payload={"targetNodeId": node_id, "shardMetadata": shard_metadata}, timeout=30)

    def delete_download(self, node_id: str, model_id: str) -> None:
        from urllib.parse import quote

        self._call("DELETE", f"/download/{quote(node_id, safe='')}/{quote(model_id, safe='')}",
                   timeout=60, allow_empty=True)

    def add_model(self, model_id: str) -> None:
        self._call("POST", "/models/add", payload={"model_id": model_id}, timeout=60)

    def search(self, query: str, limit: int = 20) -> list[SearchHit]:
        # The only exo route that answers with a bare JSON list, which the
        # shared loopback client refuses; same loopback origin, no proxy.
        import json
        from urllib import error as urlerror, request as urlrequest
        from urllib.parse import urlencode

        url = f"{self._base_url}/models/search?{urlencode({'query': query, 'limit': limit})}"
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        try:
            with opener.open(urlrequest.Request(url, headers={"Accept": "application/json"}), timeout=30) as response:
                return parse_search(json.loads(response.read()))
        except urlerror.HTTPError as error:
            raise ClusterError(f"exo request GET /models/search failed: HTTP {error.code}") from error
        except (urlerror.URLError, OSError) as error:
            raise ExoUnavailable() from error
        except ValueError as error:
            raise ClusterError("exo sent an unreadable search result") from error

    def delete_instance(self, instance_id: str) -> None:
        from urllib.parse import quote

        self._call("DELETE", f"/instance/{quote(instance_id, safe='')}", allow_empty=True)
