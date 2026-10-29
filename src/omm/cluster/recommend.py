"""Pick the biggest exo model that fits the cluster's free memory.

exo itself decides whether a placement is possible (`/instance/previews`);
omm only adds a headroom margin for context and ranks by size, because the
point of a cluster is running a model no single Mac can hold.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Mapping, Sequence

from omm.cluster.errors import PlacementError
from omm.cluster.exo_client import ClusterNode, ClusterState, ExoModel, Placement

HEADROOM = 0.85


@dataclass(frozen=True)
class Candidate:
    model_id: str
    size_bytes: int
    placement: Placement
    memory_used_fraction: float


def _available(state: ClusterState) -> int:
    return sum(max(node.memory_available_bytes or 0, 0) for node in state.nodes)


RDMA_BACKEND = "MlxJacclInstance"
SHARDINGS = ("Tensor", "Pipeline")


def _backend(placement: Placement) -> str:
    return next(iter(placement.instance), "") or (placement.instance_meta + "Instance" if placement.instance_meta else "")


def _node_count(placement: Placement) -> int:
    body = placement.instance.get(next(iter(placement.instance), ""))
    runners = body.get("shardAssignments", {}).get("nodeToRunner") if isinstance(body, dict) else None
    return len(runners) if isinstance(runners, dict) else 0


def _peak(placement: Placement) -> int:
    return max(placement.memory_delta_by_node.values(), default=0)


def link(placement: Placement) -> str:
    """How the Macs of a placement talk: "RDMA", "TCP", or "none" on a single Mac."""
    if _node_count(placement) == 1:
        # exo answers a one-Mac Jaccl request with a Ring instance that uses no network.
        return "none"
    return "RDMA" if _backend(placement) == RDMA_BACKEND else "TCP"


def _automatic(usable: Sequence[Placement]) -> Placement | None:
    if not usable:
        return None
    tensor = [p for p in usable if p.sharding == "Tensor" and _backend(p) == RDMA_BACKEND and _node_count(p) >= 2]
    if tensor:
        return min(tensor, key=_peak)
    pipeline = [p for p in usable if p.sharding == "Pipeline"]
    return min(pipeline or usable, key=lambda p: (_peak(p), _backend(p) != RDMA_BACKEND))


def choose_placement(placements: Sequence[Placement], *, sharding: str | None = None,
                     macs: int | None = None) -> Placement | None:
    """Tensor over RDMA when it fits, else the Pipeline that needs the least memory per Mac.

    Three Mac minis, Qwen3.5-9B-4bit, 2026-10-25: Tensor over RDMA (exo's
    Jaccl backend) on 2 Macs ran 72.4 tok/s, the same over TCP 49.9, and
    Pipeline on 3 Macs about 42 either way. A big model often has no Tensor
    placement at all and gets the Pipeline one as before.

    `sharding` and `macs` (the admin's `omm cluster load` flags) keep only the
    rows that match; a named sharding then prefers RDMA, then the least memory
    per Mac. exo offers one set of Macs per sharding, backend and Mac count, so
    which Macs is never a choice.
    """
    usable = [p for p in placements if p.error is None
              and (sharding is None or p.sharding.lower() == sharding.lower())
              and (macs is None or _node_count(p) == macs)]
    if sharding is None:
        return _automatic(usable)
    return min(usable, key=lambda p: (_backend(p) != RDMA_BACKEND, _peak(p)), default=None)


def resolve_node(state: ClusterState, text: str) -> ClusterNode:
    """The Mac whose name is `text` (exactly, else as a part of it, ignoring case)."""
    wanted = text.strip().lower()
    nodes = list(state.nodes)
    found = [n for n in nodes if n.name.lower() == wanted] or [n for n in nodes if wanted in n.name.lower()]
    if len(found) == 1:
        return found[0]
    names = ", ".join(n.name for n in nodes) or "none found"
    if not found:
        raise PlacementError(f"No Mac on this cluster matches {text!r}. Macs: {names}")
    raise PlacementError(f"{text!r} matches more than one Mac: {', '.join(n.name for n in found)}. Use more of the name")


def place_on(placements: Sequence[Placement], state: ClusterState, text: str) -> Placement:
    """One Pipeline copy of the model on the Mac named `text`.

    exo offers one set of Macs per sharding, backend and Mac count, and never
    lets a caller name them (/place_instance has no node argument), so omm
    takes exo's own single-Mac ring placement and moves it to that Mac. The
    ring on one Mac uses no network, so only the Mac's id changes.
    """
    node = resolve_node(state, text)
    template = next((p for p in placements if p.error is None and p.sharding == "Pipeline"
                     and p.instance_meta == "MlxRing" and _node_count(p) == 1), None)
    if template is None:
        raise PlacementError("exo offers no single-Mac placement for this model right now")
    need = _peak(template)
    if node.memory_available_bytes is not None and node.memory_available_bytes < need:
        gib = 1024 ** 3
        raise PlacementError(f"{node.name} has {node.memory_available_bytes / gib:.1f} GiB free; "
                             f"{template.model_id} needs {need / gib:.1f} GiB")
    instance = copy.deepcopy(template.instance)
    body = next(iter(instance.values()))
    (old,) = body["shardAssignments"]["nodeToRunner"]
    for table in (body["shardAssignments"]["nodeToRunner"], body.get("hostsByNode") or {}):
        if old in table:
            table[node.node_id] = table.pop(old)
    return Placement(template.model_id, "Pipeline", instance, {node.node_id: need}, None, template.instance_meta)


def _macs_text(count: int) -> str:
    return f"{count} Mac" if count == 1 else f"{count} Macs"


def describe(sharding: str | None, macs: int | None, on: str | None = None) -> str:
    """The admin's choice in words: "Tensor on 2 Macs", "Pipeline", "3 Macs", "on Node3", "automatic"."""
    if on:
        return f"on {on}"
    if sharding and macs:
        return f"{sharding} on {_macs_text(macs)}"
    return sharding or (_macs_text(macs) if macs else "automatic")


def possible(placements: Sequence[Placement]) -> str:
    """What exo offers right now, e.g. "Pipeline on 1, 2 or 3 Macs; Tensor on 1 or 2 Macs"."""
    parts = []
    for sharding in sorted({p.sharding for p in placements if p.error is None}):
        counts = sorted({_node_count(p) for p in placements if p.error is None and p.sharding == sharding} - {0})
        if not counts:
            parts.append(sharding)
            continue
        words = ", ".join(map(str, counts[:-1])) + (" or " if len(counts) > 1 else "") + str(counts[-1])
        parts.append(f"{sharding} on {words} Mac" + ("" if counts == [1] else "s"))
    return "; ".join(parts) if parts else "nothing fits the cluster's free memory now"


def placement_rows(placements: Sequence[Placement], names: Mapping[str, str] | None = None) -> list[dict]:
    """`omm cluster info`'s table: one row per distinct placement exo offers, and its refusals.

    exo lists the same one-Mac placement under both backends and repeats an
    error per backend; each shows once here. `names` (node id -> Mac name)
    adds the Macs a row uses; an employee's view leaves it out.
    """
    picked = choose_placement(placements)
    rows, seen = [], set()
    for placement in placements:
        nodes = tuple(sorted(placement.memory_delta_by_node))
        fits = placement.error is None
        key = (placement.sharding, link(placement), nodes) if fits else (placement.sharding, placement.error)
        if key in seen:
            continue
        seen.add(key)
        row = {"sharding": placement.sharding, "link": link(placement) if fits else None,
               "macs": _node_count(placement) or None, "memory_per_mac_bytes": _peak(placement) if fits else None,
               "fits": fits, "error": placement.error,
               "automatic": picked is not None and fits and key == (picked.sharding, link(picked),
                                                                     tuple(sorted(picked.memory_delta_by_node)))}
        if names is not None:
            row["nodes"] = [names.get(node, node) for node in nodes]
        rows.append(row)
    order = {"RDMA": 0, "TCP": 1, "none": 2}
    rows.sort(key=lambda r: (not r["fits"], r["sharding"], r["macs"] or 0, order.get(r["link"], 3)))
    return rows


def prefilter(models: Sequence[ExoModel], state: ClusterState) -> list[ExoModel]:
    budget = HEADROOM * _available(state)
    return [m for m in models if m.size_bytes is not None and m.size_bytes <= budget]


def rank(
    models: Sequence[ExoModel],
    previews: Mapping[str, Sequence[Placement]],
    state: ClusterState,
    limit: int = 5,
) -> list[Candidate]:
    available = _available(state)
    if available <= 0:
        return []
    candidates = []
    for model in prefilter(models, state):
        placement = choose_placement(previews.get(model.model_id, ()))
        if placement is None:
            continue
        used = sum(placement.memory_delta_by_node.values()) / available
        if used <= HEADROOM:
            candidates.append(Candidate(model.model_id, model.size_bytes or 0, placement, used))
    candidates.sort(key=lambda c: c.size_bytes, reverse=True)
    return candidates[:limit]
