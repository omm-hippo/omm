"""Pick the biggest exo model that fits the cluster's free memory.

exo itself decides whether a placement is possible (`/instance/previews`);
omm only adds a headroom margin for context and ranks by size, because the
point of a cluster is running a model no single Mac can hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from omm.cluster.exo_client import ClusterState, ExoModel, Placement

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


def _backend(placement: Placement) -> str:
    return next(iter(placement.instance), "")


def _node_count(placement: Placement) -> int:
    body = placement.instance.get(_backend(placement))
    runners = body.get("shardAssignments", {}).get("nodeToRunner") if isinstance(body, dict) else None
    return len(runners) if isinstance(runners, dict) else 0


def choose_placement(placements: Sequence[Placement]) -> Placement | None:
    """Tensor over RDMA when it fits, else the Pipeline that needs the least memory per Mac.

    Three Mac minis, Qwen3.5-9B-4bit, 2026-10-25: Tensor over RDMA (exo's
    Jaccl backend) on 2 Macs ran 72.4 tok/s, the same over TCP 49.9, and
    Pipeline on 3 Macs about 42 either way. A big model often has no Tensor
    placement at all and gets the Pipeline one as before.
    """
    usable = [p for p in placements if p.error is None]
    if not usable:
        return None
    peak = lambda p: max(p.memory_delta_by_node.values(), default=0)
    tensor = [p for p in usable if p.sharding == "Tensor" and _backend(p) == RDMA_BACKEND and _node_count(p) >= 2]
    if tensor:
        return min(tensor, key=peak)
    pipeline = [p for p in usable if p.sharding == "Pipeline"]
    return min(pipeline or usable, key=lambda p: (peak(p), _backend(p) != RDMA_BACKEND))


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
