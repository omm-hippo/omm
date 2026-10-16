#!/usr/bin/env python3
"""Decide whether a computed arena leaderboard may be published.

Sub-project C's counterpart to scripts/model_quality_gate.py. A blocked scope
is a success, not a failed workflow: the nightly job must not go red because
the corpus is thin. Since sub-project D (#410) each scope - the overall board
and every memory group - is gated on its own; a blocked scope keeps its last
ranking, or, if it never had one, is published as "collecting" with its real
counts (see `publish`).

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

from dataclasses import dataclass

#: A leaderboard fitted from a handful of battles is noise presented as fact.
MIN_EFFECTIVE_VOTES = 200

#: The telemetry corpus is already overwhelmingly one machine. Catch the same
#: failure here before publication, not after.
MAX_SINGLE_CLIENT_SHARE = 0.5

MIN_RANKED_MODELS = 5

#: A memory group is a slice of the corpus, so it gets lower bars than the
#: overall board - but the same single-machine share limit: a group one
#: machine can set is exactly the ranking this gate exists to prevent.
MIN_GROUP_EFFECTIVE_VOTES = 100
MIN_GROUP_RANKED_MODELS = 3

#: A sudden collapse in ranked models is a corpus or parsing regression, not a
#: real change in what people run.
MAX_RANKED_MODEL_DROP = 0.3


@dataclass(frozen=True)
class GateResult:
    status: str
    reasons: tuple[str, ...]


def ranked_model_count(artifact: dict) -> int:
    """Models that actually carry a tier. Provisional entries do not count."""
    models = artifact.get("models")
    if not isinstance(models, list):
        return 0
    return sum(1 for model in models if not model.get("provisional", True))


def evaluate(
    artifact: dict,
    incumbent: dict | None = None,
    *,
    min_votes: float = MIN_EFFECTIVE_VOTES,
    min_ranked: int = MIN_RANKED_MODELS,
) -> GateResult:
    """Every failing condition is reported, not just the first one.

    `artifact` is one scope - the overall board or one memory group - and
    `incumbent` the same scope as last published, if it was ranked.
    """
    corpus = artifact.get("corpus") or {}
    reasons: list[str] = []

    votes = float(corpus.get("effective_votes") or 0.0)
    if votes < min_votes:
        reasons.append(f"only {votes:.1f} effective votes, need {min_votes:g}")

    share = float(corpus.get("largest_client_share") or 0.0)
    if share > MAX_SINGLE_CLIENT_SHARE:
        reasons.append(
            f"one client holds {share:.0%} of the effective weight, "
            f"limit {MAX_SINGLE_CLIENT_SHARE:.0%}"
        )

    ranked = ranked_model_count(artifact)
    if ranked < min_ranked:
        reasons.append(f"only {ranked} ranked models, need {min_ranked}")

    previous = ranked_model_count(incumbent) if incumbent else 0
    if previous > 0:
        drop = (previous - ranked) / previous
        if drop > MAX_RANKED_MODEL_DROP:
            reasons.append(
                f"ranked models shrank {drop:.0%} against the incumbent "
                f"({previous} -> {ranked}), limit {MAX_RANKED_MODEL_DROP:.0%}"
            )

    return GateResult(status="blocked" if reasons else "passed", reasons=tuple(reasons))


# --- publication (sub-project D) ------------------------------------------

#: What a scope says about its votes. Compared to decide whether a scope's
#: content changed; the three keys below are bookkeeping about publication.
SCOPE_FIELDS = (
    "corpus", "models", "tiers", "efficiency_components", "provisional_models", "pairs",
)
_PUBLICATION_FIELDS = ("ranked", "reasons", "updated_at")
_GROUP_IDENTITY = ("key", "memory_kind", "memory_bucket")


def _content(scope: dict) -> dict:
    return {key: scope.get(key) for key in SCOPE_FIELDS}


def _decide(
    fresh: dict | None,
    incumbent: dict | None,
    *,
    generated_at: str,
    gated: bool,
    min_votes: float,
    min_ranked: int,
) -> tuple[dict, GateResult]:
    """One scope's published form.

    passed -> the fresh scope; blocked but ranked before -> the previous
    ranking, untouched ("keep the last good board", per scope); blocked and
    never ranked -> its real counts and the gate's reasons, no models.
    """
    previous = incumbent if incumbent and incumbent.get("ranked") is True else None
    if fresh is None:
        result = GateResult(status="blocked", reasons=("no votes in this scope this run",))
    elif not gated:
        result = GateResult(status="passed", reasons=())
    else:
        result = evaluate(fresh, previous, min_votes=min_votes, min_ranked=min_ranked)

    if result.status == "passed" and fresh is not None:
        content = _content(fresh)
        unchanged = previous is not None and _content(previous) == content
        return (
            {
                **content,
                "ranked": True,
                "reasons": [],
                "updated_at": previous["updated_at"] if unchanged else generated_at,
            },
            result,
        )
    if previous is not None:
        return {key: previous.get(key) for key in SCOPE_FIELDS + _PUBLICATION_FIELDS}, result
    return (
        {
            "corpus": (fresh or {}).get("corpus") or {},
            "models": [],
            "tiers": [],
            "efficiency_components": [],
            "provisional_models": [],
            "pairs": [],
            "ranked": False,
            "reasons": list(result.reasons),
            "updated_at": None,
        },
        result,
    )


def publish(
    fresh: dict, incumbent: dict | None, *, gated: bool = True
) -> tuple[dict, dict[str, GateResult]]:
    """The artifact to publish, from a fresh v2 build and the last published
    file, plus every scope's gate result keyed "overall" / group key.

    Overall and every memory group are gated independently, so a group can
    rank while overall is still collecting. A scope that ranked before is
    never blanked by a blocked run - which is also why a collecting file can
    never replace a ranked one. `gated=False` is for local previews only.

    An incumbent that is not schema 2 is not merged: its scopes cannot be
    matched to this shape. The caller refuses to write a collecting file over
    it (aggregate_arena_votes.main).
    """
    generated_at = fresh["generated_at"]
    previous = (
        incumbent
        if isinstance(incumbent, dict) and incumbent.get("schema_version") == 2
        else None
    )
    results: dict[str, GateResult] = {}

    overall, results["overall"] = _decide(
        {key: fresh.get(key) for key in SCOPE_FIELDS},
        {key: previous.get(key) for key in SCOPE_FIELDS + _PUBLICATION_FIELDS}
        if previous
        else None,
        generated_at=generated_at,
        gated=gated,
        min_votes=MIN_EFFECTIVE_VOTES,
        min_ranked=MIN_RANKED_MODELS,
    )

    fresh_groups = {group["key"]: group for group in fresh.get("groups") or []}
    previous_groups = {
        group["key"]: group
        for group in (previous or {}).get("groups") or []
        if isinstance(group, dict) and isinstance(group.get("key"), str)
    }
    groups = []
    for key in sorted(set(fresh_groups) | {k for k, g in previous_groups.items() if g.get("ranked")}):
        source = fresh_groups.get(key) or previous_groups[key]
        scope, results[key] = _decide(
            fresh_groups.get(key),
            previous_groups.get(key),
            generated_at=generated_at,
            gated=gated,
            min_votes=MIN_GROUP_EFFECTIVE_VOTES,
            min_ranked=MIN_GROUP_RANKED_MODELS,
        )
        groups.append({**{name: source[name] for name in _GROUP_IDENTITY}, **scope})

    any_ranked = overall["ranked"] or any(group["ranked"] for group in groups)
    return (
        {
            "schema_version": 2,
            "generated_at": generated_at,
            "status": "ranked" if any_ranked else "collecting",
            **overall,
            "groups": groups,
        },
        results,
    )
