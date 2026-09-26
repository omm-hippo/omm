#!/usr/bin/env python3
"""Arena leaderboard scoring math (sub-project C, issue #409).

Pure functions over vote rows: validation, abuse weighting, a weighted
Bradley-Terry quality fit with confidence-interval tiers, and a
machine-independent efficiency rating fitted from within-battle log-ratios.

No I/O, no network, no dependency outside the standard library. The runtime
package never imports this module - it only reads the artifact this produces,
the same boundary that keeps scikit-learn out of omm's runtime dependencies.

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

import math
import random
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

#: One (client_id, unordered model pair) group contributes at most this many
#: effective votes. Rows are never discarded, only down-weighted.
CLIENT_PAIR_VOTE_CAP = 10

#: Below this much effective evidence a Bradley-Terry strength is mostly the
#: prior, so the model is reported as provisional and gets no tier.
MIN_EFFECTIVE_BATTLES = 20

#: "Fails outright about a third of the time."
BOTH_BAD_WARNING_RATE = 0.30

#: Virtual wins and losses against a phantom opponent of fixed strength 1.
#: Keeps the MLE finite for an undefeated or never-winning model, connects the
#: comparison graph, and shrinks thin records toward the middle.
BT_PRIOR_STRENGTH = 0.5

BOOTSTRAP_RESAMPLES = 200

#: Fixed so an unchanged corpus produces a byte-identical artifact. A nightly
#: job that commits bootstrap noise is worse than no job.
BOOTSTRAP_SEED = 20260926

#: Below this a memory reading is not a plausible loaded-model footprint.
MIN_MEMORY_GB = 0.05

MAX_ITERATIONS = 500
TOLERANCE = 1e-10

WINNERS = frozenset({"a", "b", "both_bad"})
ENGINES = frozenset({"ollama", "lmstudio"})
SIDES = ("a", "b")

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# Mirrors the ranges database.rules.json already enforces. The artifact must
# not trust the corpus: the rules constrain what can be written today, not what
# is already stored or what a future rule change might let through.
_MAX_ELAPSED = 3600
_MAX_TOKENS = 1_000_000


@dataclass(frozen=True)
class Battle:
    """One validated vote.

    `efficiency_*` is None when the row cannot be used for the efficiency
    axis; the vote itself still counts for quality.
    """

    battle_id: str
    recorded_at: str
    client_id: str
    key_a: str
    key_b: str
    winner: str
    efficiency_a: float | None
    efficiency_b: float | None

    @property
    def pair(self) -> frozenset[str]:
        return frozenset({self.key_a, self.key_b})


def model_key(row: dict, side: str) -> str | None:
    """Stable identity for one side of a battle.

    sha256 when the row carries a digest, otherwise the normalized filename.
    The prefix is part of the key so a consumer never has to guess which
    fallback produced an entry.
    """
    digest = row.get(f"model_digest_{side}")
    if isinstance(digest, str) and _DIGEST_RE.match(digest):
        return f"sha256:{digest}"
    filename = row.get(f"model_filename_{side}")
    if isinstance(filename, str) and filename.strip():
        return f"filename:{filename.strip().lower()}"
    return None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _bounded_number(value: object, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def _efficiency(row: dict, side: str) -> float | None:
    """tokens/sec per GiB of measured footprint, or None when unusable.

    Uses tokens_per_second, never tokens/elapsed: elapsed is wall clock and
    includes the model load, so it is not decode speed.
    """
    speed = _positive_number(row.get(f"tokens_per_second_{side}"))
    memory = _positive_number(row.get(f"memory_gb_{side}"))
    if speed is None or memory is None or memory < MIN_MEMORY_GB:
        return None
    return speed / memory


def validate_rows(rows: list[dict]) -> tuple[list[Battle], dict[str, int]]:
    """Turn raw vote rows into Battles, reporting why each dropped row dropped.

    Rows are sorted by (recorded_at, battle_id) first: the RTDB export is a
    dict whose iteration order carries no meaning, and the bootstrap has to be
    reproducible.
    """
    dropped: Counter[str] = Counter()

    def sort_key(row: dict) -> tuple[str, str]:
        recorded_at = row.get("recorded_at")
        battle_id = row.get("battle_id")
        return (
            recorded_at if isinstance(recorded_at, str) else "",
            battle_id if isinstance(battle_id, str) else "",
        )

    battles: list[Battle] = []
    seen_battle_ids: set[str] = set()
    for row in sorted(rows, key=sort_key):
        if row.get("schema_version") != 1:
            dropped["schema_version"] += 1
            continue
        winner = row.get("winner")
        if winner not in WINNERS:
            dropped["winner"] += 1
            continue
        if row.get("engine") not in ENGINES:
            dropped["engine"] += 1
            continue
        battle_id = row.get("battle_id")
        client_id = row.get("client_id")
        recorded_at = row.get("recorded_at")
        if not (
            isinstance(battle_id, str)
            and battle_id
            and isinstance(client_id, str)
            and client_id
            and isinstance(recorded_at, str)
            and recorded_at
        ):
            dropped["metadata"] += 1
            continue
        if battle_id in seen_battle_ids:
            # B's queue can re-send a row after a partial flush, and the RTDB
            # key is a PoW digest, not the battle id.
            dropped["duplicate_battle_id"] += 1
            continue
        key_a = model_key(row, "a")
        key_b = model_key(row, "b")
        if key_a is None or key_b is None:
            dropped["identity"] += 1
            continue
        if key_a == key_b:
            dropped["self_battle"] += 1
            continue
        if any(
            _bounded_number(row.get(f"elapsed_{side}"), _MAX_ELAPSED) is None
            or _bounded_number(row.get(f"tokens_{side}"), _MAX_TOKENS) is None
            for side in SIDES
        ):
            dropped["timing"] += 1
            continue

        efficiency_a = _efficiency(row, "a")
        efficiency_b = _efficiency(row, "b")
        if efficiency_a is None or efficiency_b is None:
            # The axis needs both sides: its whole point is the ratio between
            # two models measured on the same machine.
            efficiency_a = efficiency_b = None

        seen_battle_ids.add(battle_id)
        battles.append(
            Battle(
                battle_id=battle_id,
                recorded_at=recorded_at,
                client_id=client_id,
                key_a=key_a,
                key_b=key_b,
                winner=winner,
                efficiency_a=efficiency_a,
                efficiency_b=efficiency_b,
            )
        )
    return battles, dict(dropped)


def battle_weights(battles: list[Battle]) -> list[float]:
    """Per-row weight capping what one machine can say about one pair.

    A client that voted three times on a pair keeps full weight. One that voted
    a thousand times on the same pair still contributes CLIENT_PAIR_VOTE_CAP
    effective votes in total.
    """
    sizes = Counter((battle.client_id, battle.pair) for battle in battles)
    return [
        min(1.0, CLIENT_PAIR_VOTE_CAP / sizes[(battle.client_id, battle.pair)])
        for battle in battles
    ]


def fit_quality(
    battles: list[Battle],
    weights: list[float],
    *,
    prior: float = BT_PRIOR_STRENGTH,
) -> dict[str, float]:
    """Weighted Bradley-Terry log-strengths, one per model key.

    Minorization-maximization iteration:

        p_i  <-  (W_i + prior) / ( sum_j n_ij/(p_i + p_j) + 2*prior/(p_i + 1) )

    where W_i is i's total weighted wins and n_ij the total weighted
    comparisons between i and j. `both_bad` rows are excluded from the fit and
    tracked separately by `both_bad_rates`.

    The trailing term is a phantom opponent of fixed strength 1 carrying
    `prior` wins and `prior` losses for every model. It keeps the MLE finite
    for an undefeated or never-winning model, connects the comparison graph so
    this axis needs no component handling, and shrinks thin records toward the
    middle. Because the phantom's strength is fixed, the scale is already
    identified - do not renormalize the result.
    """
    keys = sorted({key for battle in battles for key in (battle.key_a, battle.key_b)})
    if not keys:
        return {}

    wins: dict[str, float] = {key: 0.0 for key in keys}
    comparisons: dict[str, dict[str, float]] = {key: defaultdict(float) for key in keys}
    for battle, weight in zip(battles, weights):
        if battle.winner == "a":
            winner, loser = battle.key_a, battle.key_b
        elif battle.winner == "b":
            winner, loser = battle.key_b, battle.key_a
        else:
            continue
        wins[winner] += weight
        comparisons[winner][loser] += weight
        comparisons[loser][winner] += weight

    strengths = {key: 1.0 for key in keys}
    for _ in range(MAX_ITERATIONS):
        change = 0.0
        for key in keys:
            own = strengths[key]
            denominator = 2.0 * prior / (own + 1.0)
            for opponent, total in comparisons[key].items():
                denominator += total / (own + strengths[opponent])
            updated = (wins[key] + prior) / denominator
            change = max(change, abs(updated - own) / max(own, 1e-12))
            strengths[key] = updated
        if change < TOLERANCE:
            break
    return {key: math.log(value) for key, value in strengths.items()}


def effective_battles(battles: list[Battle], weights: list[float]) -> dict[str, float]:
    """Total weight each model took part in, on either side."""
    totals: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        totals[battle.key_a] += weight
        totals[battle.key_b] += weight
    return dict(totals)


def both_bad_rates(battles: list[Battle], weights: list[float]) -> dict[str, float]:
    """Share of a model's weighted battles the user rejected outright.

    Reported next to the tier, never folded into it: "wins its matchups" and
    "often fails outright" are different facts about a model.
    """
    totals: dict[str, float] = defaultdict(float)
    rejected: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        for key in (battle.key_a, battle.key_b):
            totals[key] += weight
            if battle.winner == "both_bad":
                rejected[key] += weight
    return {
        key: (rejected[key] / total if total else 0.0) for key, total in totals.items()
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_intervals(
    battles: list[Battle],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, tuple[float, float]]:
    """95% percentile intervals on each model's log-strength.

    Resamples battles with replacement and refits, recomputing the abuse
    weights inside each resample so a resample that happens to repeat one
    client's rows is capped the same way the real corpus is.

    The seed is fixed: the nightly job commits this artifact, so an unchanged
    corpus has to produce an unchanged file.
    """
    keys = sorted({key for battle in battles for key in (battle.key_a, battle.key_b)})
    if not battles or not keys:
        return {}
    point = fit_quality(battles, battle_weights(battles))
    samples: dict[str, list[float]] = {key: [] for key in keys}
    generator = random.Random(seed)
    size = len(battles)
    for _ in range(resamples):
        picked = [battles[generator.randrange(size)] for _ in range(size)]
        fitted = fit_quality(picked, battle_weights(picked))
        for key, strength in fitted.items():
            samples[key].append(strength)
    intervals: dict[str, tuple[float, float]] = {}
    for key in keys:
        drawn = samples[key]
        if len(drawn) < 2:
            # The model appeared in almost no resample; there is nothing to
            # spread. Its effective battle count will mark it provisional.
            intervals[key] = (point[key], point[key])
            continue
        intervals[key] = (_percentile(drawn, 0.025), _percentile(drawn, 0.975))
    return intervals


def assign_tiers(ranked: list[tuple[str, float, float, float]]) -> dict[str, int]:
    """Group models whose interval overlaps their tier leader's.

    `ranked` is (key, strength, ci_low, ci_high), descending by strength.

    The anchor is the tier's leader, not the previous model. Interval overlap
    is not transitive - A overlaps B and B overlaps C while A and C are
    disjoint - so chaining would let one tier grow without bound. Anchoring on
    the leader makes a tier mean "not distinguishable from this tier's best",
    which holds for every member.
    """
    tiers: dict[str, int] = {}
    leader_low: float | None = None
    tier = 0
    for key, _strength, ci_low, ci_high in ranked:
        if leader_low is None or ci_high < leader_low:
            tier += 1
            leader_low = ci_low
        tiers[key] = tier
    return tiers


def _components(adjacency: dict[str, list[tuple[str, float, float]]]) -> dict[str, int]:
    """Connected components of the efficiency graph, numbered by size.

    Component 0 is the largest (ties broken by lowest member key), so the
    consumer's "main board" is stable across runs.
    """
    seen: set[str] = set()
    groups: list[list[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        group = []
        while stack:
            key = stack.pop()
            group.append(key)
            for neighbour, _ratio, _weight in adjacency[key]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        groups.append(sorted(group))
    groups.sort(key=lambda group: (-len(group), group[0]))
    return {key: index for index, group in enumerate(groups) for key in group}


def fit_efficiency(battles: list[Battle], weights: list[float]) -> dict[str, dict]:
    """Per-model efficiency ratings fitted from within-battle log-ratios.

    tokens/sec per GiB on its own measures the machine, not the model: the same
    model scores 30 on one GPU and 7.5 on another. Both sides of an arena row
    were measured on the same machine in the same session, so the ratio between
    them cancels the machine - 30/20 and 7.5/5.0 are both 1.5.

    For each eligible row, r = ln(e_a) - ln(e_b). Ratings f minimize
    sum(weight * (r - (f_a - f_b))^2), solved by weighted iterative averaging.
    Only differences are determined, so each connected component is centered on
    its own weighted mean and ratings from different components are not
    comparable - hence the reported `component`.
    """
    adjacency: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    observations: dict[str, list[float]] = defaultdict(list)
    mass: dict[str, float] = defaultdict(float)
    for battle, weight in zip(battles, weights):
        if battle.efficiency_a is None or battle.efficiency_b is None:
            continue
        ratio = math.log(battle.efficiency_a) - math.log(battle.efficiency_b)
        adjacency[battle.key_a].append((battle.key_b, ratio, weight))
        adjacency[battle.key_b].append((battle.key_a, -ratio, weight))
        observations[battle.key_a].append(battle.efficiency_a)
        observations[battle.key_b].append(battle.efficiency_b)
        mass[battle.key_a] += weight
        mass[battle.key_b] += weight
    if not adjacency:
        return {}

    keys = sorted(adjacency)
    ratings = {key: 0.0 for key in keys}
    for _ in range(MAX_ITERATIONS):
        change = 0.0
        for key in keys:
            numerator = 0.0
            denominator = 0.0
            for neighbour, ratio, weight in adjacency[key]:
                numerator += weight * (ratings[neighbour] + ratio)
                denominator += weight
            updated = numerator / denominator
            change = max(change, abs(updated - ratings[key]))
            ratings[key] = updated
        if change < TOLERANCE:
            break

    component_of = _components(adjacency)
    grouped: dict[int, list[str]] = defaultdict(list)
    for key in keys:
        grouped[component_of[key]].append(key)
    for members in grouped.values():
        total = sum(mass[key] for key in members)
        centre = (
            sum(ratings[key] * mass[key] for key in members) / total if total else 0.0
        )
        for key in members:
            ratings[key] -= centre

    return {
        key: {
            "rating": ratings[key],
            "component": component_of[key],
            "raw_median_tok_s_per_gb": statistics.median(observations[key]),
            "sample": len(observations[key]),
        }
        for key in keys
    }
