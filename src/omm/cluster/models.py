"""Cluster models the omm way: search, install, list, inspect, uninstall.

exo keeps two separate things per model: the files each Mac downloaded
(`/state` `downloads`) and the instance that holds it in memory. `install`
and `uninstall` handle only the files; loading stays in commands.load_model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from omm.cluster import commands, exo_client, recommend
from omm.cluster.errors import ClusterError
from omm.cluster.exo_client import ExoClient

Out = commands.Out
_GIB = 1024**3
# exo creates the Pending entries of a model it was just told about a moment later.
ENTRY_WAIT_SECONDS = 30


@dataclass(frozen=True)
class ModelRow:
    model: str
    size_bytes: int | None
    nodes_installed: int
    nodes_total: int
    # loaded | loading | installed | partial | downloading | failed
    state: str

    def as_dict(self) -> dict:
        return asdict(self)


def _rows_for(downloads: list[exo_client.NodeDownload], model_id: str) -> dict[str, exo_client.NodeDownload]:
    return {row.node_id: row for row in downloads if row.model_id == model_id}


def _names(state: exo_client.ClusterState) -> dict[str, str]:
    return {node.node_id: node.name for node in state.nodes}


def inventory(client: ExoClient | None = None) -> list[ModelRow]:
    """Every model at least one Mac has (or is fetching), plus loaded ones."""
    commands.require_macos()
    client = client or ExoClient()
    raw = client.raw_state()
    state = exo_client.parse_state(raw)
    downloads = [row for row in exo_client.parse_downloads(raw) if row.kind != "Pending"]
    loaded = {instance.model_id for instance in state.instances}
    rows = []
    for model_id in sorted({row.model_id for row in downloads} | loaded):
        mine = _rows_for(downloads, model_id).values()
        installed = sum(1 for row in mine if row.kind == "Completed")
        sizes = [row.total_bytes for row in mine if row.total_bytes]
        if model_id in loaded:
            status = "loaded" if exo_client.instance_ready(raw, model_id) else "loading"
        elif any(row.kind == "Failed" for row in mine):
            status = "failed"
        elif any(row.kind == "Ongoing" for row in mine):
            status = "downloading"
        else:
            status = "installed" if installed >= len(state.nodes) else "partial"
        rows.append(ModelRow(model_id, max(sizes) if sizes else None, installed, len(state.nodes), status))
    return rows


def install(model_id: str, out: Out, client: ExoClient | None = None, *, sleep=None, clock=None) -> int:
    """Download `model_id` to every Mac without loading it; return how many Macs started."""
    import time

    commands.require_head_for("omm cluster install")
    client = client or ExoClient()
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    if model_id not in {model.model_id for model in client.models()}:
        out(f"Adding {model_id} to exo's model list...")
        client.add_model(model_id)
    deadline = clock() + ENTRY_WAIT_SECONDS
    while True:
        raw = client.raw_state()
        state = exo_client.parse_state(raw)
        mine = _rows_for(exo_client.parse_downloads(raw), model_id)
        if mine or clock() >= deadline:
            break
        sleep(2)
    if not mine:
        raise ClusterError(f"exo has no download for {model_id}; check the id with: omm cluster search <text>")
    # Every entry carries the same whole-model shard, so any one serves a Mac that has none yet.
    shard = next(iter(mine.values())).shard_metadata
    missing = [node.node_id for node in state.nodes
               if node.node_id not in mine or mine[node.node_id].kind in ("Pending", "Failed")]
    if not missing and all(row.kind == "Completed" for row in mine.values()):
        out(f"{model_id} is already on every Mac.")
        return 0
    for node_id in missing:
        client.start_download(node_id, mine[node_id].shard_metadata if node_id in mine else shard)
    _await_downloads(client, model_id, out, sleep=sleep, clock=clock)
    return len(missing)


def _await_downloads(client: ExoClient, model_id: str, out: Out, *, sleep, clock) -> None:
    last_key, last_change, signature = None, clock(), None
    while True:
        raw = client.raw_state()
        names = _names(exo_client.parse_state(raw))
        mine = list(_rows_for(exo_client.parse_downloads(raw), model_id).values())
        failed = next((row for row in mine if row.kind == "Failed"), None)
        if failed is not None:
            raise ClusterError(f"{names.get(failed.node_id, failed.node_id)} could not download {model_id}: "
                               f"{failed.error}. Fix the cause, run omm cluster restart on that Mac, "
                               f"then run omm cluster install {model_id} again.")
        unfinished = [row for row in mine if row.kind != "Completed"]
        if not unfinished:
            return
        sized = [row for row in unfinished if row.total_bytes]
        if sized:
            slowest = min(sized, key=lambda row: row.done_bytes / row.total_bytes)
            percent = slowest.done_bytes * 100 // slowest.total_bytes
            key = percent // 5
            line = (f"Downloading {model_id}: {percent}% ({slowest.done_bytes / _GIB:.1f} of "
                    f"{slowest.total_bytes / _GIB:.1f} GiB on the slowest Mac)")
        else:
            key, line = None, f"Waiting for exo to start downloading {model_id}..."
        if key != last_key:
            last_key = key
            out(line)
        now = clock()
        moved = (sum(row.done_bytes for row in mine), len(unfinished))
        if moved != signature:
            signature, last_change = moved, now
        elif now - last_change >= commands.MODEL_STALL_TIMEOUT:
            raise ClusterError(f"{model_id} made no progress for {commands.MODEL_STALL_TIMEOUT // 60} minutes. "
                               f"Last status: {line}")
        sleep(2)


def uninstall(model_id: str, out: Out, client: ExoClient | None = None) -> int:
    """Unload `model_id` if it is loaded, then delete its files from every Mac."""
    commands.require_head_for("omm cluster uninstall")
    client = client or ExoClient()
    unloaded = commands.unload_model(model_id, client)
    if unloaded:
        out(f"Stopped {unloaded} running copy(ies) of {model_id}.")
    raw = client.raw_state()
    present = [row for row in _rows_for(exo_client.parse_downloads(raw), model_id).values() if row.kind != "Pending"]
    if not present and not unloaded:
        raise ClusterError(f"{model_id} is not installed on this cluster (see omm cluster list)")
    for row in present:
        client.delete_download(row.node_id, model_id)
    return len(present)


def info(model_id: str, client: ExoClient | None = None) -> dict:
    commands.require_macos()
    client = client or ExoClient()
    catalog = {model.model_id: model for model in client.models()}
    raw = client.raw_state()
    state = exo_client.parse_state(raw)
    mine = _rows_for(exo_client.parse_downloads(raw), model_id)
    if model_id not in catalog and not any(row.kind != "Pending" for row in mine.values()):
        raise ClusterError(f"exo does not know {model_id}; find the exact id with: omm cluster search <text>")
    nodes = []
    for node in state.nodes:
        row = mine.get(node.node_id)
        kind = row.kind if row else "Pending"
        nodes.append({"name": node.name,
                      "state": {"Completed": "installed", "Pending": "missing"}.get(kind, kind.lower())})
    instance = next((i for i in state.instances if i.model_id == model_id), None)
    fits, peak = None, None
    if instance is None:
        placement = recommend.choose_placement(client.previews(model_id))
        fits = placement is not None
        peak = max(placement.memory_delta_by_node.values(), default=0) if placement else None
    model = catalog.get(model_id)
    sizes = [row.total_bytes for row in mine.values() if row.total_bytes]
    return {
        "model": model_id,
        "card": dict(model.card) if model else {},
        "size_bytes": (model.size_bytes if model and model.size_bytes else max(sizes) if sizes else None),
        "nodes": nodes,
        "loaded": instance is not None,
        "sharding": instance.sharding if instance else None,
        "fits": True if instance is not None else fits,
        "peak_node_bytes": peak,
    }


def search(query: str, client: ExoClient | None = None) -> list[dict]:
    commands.require_macos()
    client = client or ExoClient()
    hits = client.search(query)
    catalog = {model.model_id: model for model in client.models()}
    state = client.state()
    budget = recommend.HEADROOM * sum(max(node.memory_available_bytes or 0, 0) for node in state.nodes)
    rows = []
    for hit in hits:
        model = catalog.get(hit.model_id)
        size = model.size_bytes if model else None
        rows.append({"model": hit.model_id, "downloads": hit.downloads, "in_catalog": model is not None,
                     "size_bytes": size, "fits": None if size is None else size <= budget})
    return rows
