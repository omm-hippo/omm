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
    return sum(max(node.memory_available_bytes, 0) for node in state.nodes)


def choose_placement(placements: Sequence[Placement]) -> Placement | None:
    usable = [p for p in placements if p.error is None]
    if not usable:
        return None
    peak = lambda p: max(p.memory_delta_by_node.values(), default=0)
    pipeline = [p for p in usable if p.sharding == "Pipeline"]
    return min(pipeline or usable, key=peak)


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
