#!/usr/bin/env python3
"""Decide whether a computed arena leaderboard may be published.

Sub-project C's counterpart to scripts/model_quality_gate.py. A blocked run is
a success that publishes nothing, not a failed workflow: the nightly job must
not go red because the corpus is thin.

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


def evaluate(artifact: dict, incumbent: dict | None = None) -> GateResult:
    """Every failing condition is reported, not just the first one."""
    corpus = artifact.get("corpus") or {}
    reasons: list[str] = []

    votes = float(corpus.get("effective_votes") or 0.0)
    if votes < MIN_EFFECTIVE_VOTES:
        reasons.append(f"only {votes:.1f} effective votes, need {MIN_EFFECTIVE_VOTES}")

    share = float(corpus.get("largest_client_share") or 0.0)
    if share > MAX_SINGLE_CLIENT_SHARE:
        reasons.append(
            f"one client holds {share:.0%} of the effective weight, "
            f"limit {MAX_SINGLE_CLIENT_SHARE:.0%}"
        )

    ranked = ranked_model_count(artifact)
    if ranked < MIN_RANKED_MODELS:
        reasons.append(f"only {ranked} ranked models, need {MIN_RANKED_MODELS}")

    previous = ranked_model_count(incumbent) if incumbent else 0
    if previous > 0:
        drop = (previous - ranked) / previous
        if drop > MAX_RANKED_MODEL_DROP:
            reasons.append(
                f"ranked models shrank {drop:.0%} against the incumbent "
                f"({previous} -> {ranked}), limit {MAX_RANKED_MODEL_DROP:.0%}"
            )

    return GateResult(status="blocked" if reasons else "passed", reasons=tuple(reasons))
