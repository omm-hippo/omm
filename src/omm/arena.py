"""Local blind model battles (`omm arena`).

Sub-project A of the arena roadmap: pairing, blind generation, vote capture,
and a local-only vote log. Nothing here uploads anything; `votes.jsonl` is
read by sub-project B when that lands, never by this module.

Design: docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import Timeout as FileLockTimeout

from . import config, linker, quality
from .atomic import locked

logger = logging.getLogger("omm.arena")

WINNERS = frozenset({"a", "b", "both_bad"})


def arena_dir() -> Path:
    """Resolved at call time, never at import: OMM_HOME is overridable and
    the test fixtures monkeypatch `config.OMM_HOME` (see runlog._logs_dir)."""
    return config.OMM_HOME / "arena"


def votes_path() -> Path:
    return arena_dir() / "votes.jsonl"


def build_vote_row(
    *,
    prompt: str,
    model_a: str,
    model_b: str,
    engine: str,
    result_a,
    result_b,
    winner: str,
    kept: bool,
) -> dict:
    """One `votes.jsonl` row, in exactly the shape sub-project B will upload.

    `engine` is a single value because a session never mixes engines; the
    row still carries engine_a/engine_b separately so a later cross-engine
    mode needs no schema migration. `watt_*` is always None in sub-project A.
    """
    if winner not in WINNERS:
        raise ValueError(f"winner must be one of {sorted(WINNERS)}, got {winner!r}")
    return {
        "battle_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "model_a": model_a,
        "model_b": model_b,
        "engine_a": engine,
        "engine_b": engine,
        "elapsed_a": round(float(result_a.elapsed), 3),
        "elapsed_b": round(float(result_b.elapsed), 3),
        "tokens_a": int(result_a.tokens),
        "tokens_b": int(result_b.tokens),
        "memory_gb_a": result_a.memory_gb,
        "memory_gb_b": result_b.memory_gb,
        "watt_a": None,
        "watt_b": None,
        "winner": winner,
        "pinned": bool(kept),
    }


def append_vote(row: dict) -> bool:
    """Append one JSON line. Returns False (never raises) on any filesystem
    or lock failure: the vote is already cast and revealed by the time this
    runs, so a write failure must not take the session down with it."""
    path = votes_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, FileLockTimeout, ValueError, TypeError) as error:
        logger.debug("arena vote append failed: %s", error)
        return False
    logger.info("arena vote recorded", extra={"winner": row.get("winner")})
    return True


class ArenaError(RuntimeError):
    """A user-facing arena setup problem; cli.py prints str(error)."""


def validate_seed_pair(models: list[str], pool: list[str]) -> tuple[str, str]:
    if len(models) != 2:
        raise ArenaError(
            "`omm arena` takes two models or none at all - pass two models to "
            "seed the first round, or no models to draw a random pair."
        )
    left, right = models
    if left == right:
        raise ArenaError("Pass two different models; a model cannot battle itself.")
    missing = [m for m in (left, right) if m not in pool]
    if missing:
        raise ArenaError("Not available for this arena session: " + ", ".join(missing))
    return left, right


class Pairing:
    """Which two models face off in each round.

    A seed pair applies to round 1 only unless `keep` is set; `keep` freezes
    whatever round 1 ended up using (seeded or drawn) for the whole session.
    """

    def __init__(
        self,
        pool: list[str],
        seed_pair: tuple[str, str] | None = None,
        keep: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        if len(set(pool)) < 2:
            raise ArenaError("An arena round needs at least 2 distinct models.")
        self._pool = list(pool)
        self._seed_pair = seed_pair
        self._keep = keep
        self._rng = rng or random.Random()
        self._held: tuple[str, str] | None = None

    def next_pair(self) -> tuple[str, str]:
        if self._held is not None:
            return self._held
        if self._seed_pair is not None:
            pair = self._seed_pair
            self._seed_pair = None
        else:
            # sample() draws without replacement and returns them in a random
            # order, so neither slot is biased toward any model.
            pair = tuple(self._rng.sample(self._pool, 2))
        if self._keep:
            self._held = pair
        return pair


@dataclass(frozen=True)
class GenerationResult:
    text: str
    elapsed: float
    tokens: int
    tokens_per_second: float | None
    memory_gb: float | None


def generate_side(
    model_ref: str,
    prompt: str,
    *,
    engine: str,
    lmstudio_port: int | None,
) -> GenerationResult:
    """One side of a round: load-by-generating, time it, read memory, unload.

    Sampling is the model's own default (`generation=None`) - arena compares
    what a user would actually get, not a reproducible fixture. Extended
    thinking is never displayed or stored: Ollama returns the trace in a
    separate `thinking` field, and only `response` is read here.

    Always unloads afterwards, including on failure, so the next side gets a
    clean machine. Raises QualityEvaluationError with the engine's existing
    FailureReason on any load/generate failure; the caller aborts the round.
    """
    started = time.monotonic()
    try:
        if engine == "lmstudio":
            data = quality._generate_lmstudio(model_ref, prompt, None, None, lmstudio_port)
        else:
            data = quality._generate(model_ref, prompt, None)
        elapsed = time.monotonic() - started
        memory_gb = None if engine == "lmstudio" else quality.loaded_model_memory_gb(model_ref)
    finally:
        if engine == "lmstudio":
            linker.unload_lmstudio_model(model_ref)
        else:
            quality.ensure_model_unloaded(model_ref)
    tokens = data.get("eval_count")
    return GenerationResult(
        text=str(data.get("response", "")).strip(),
        elapsed=elapsed,
        tokens=int(tokens) if isinstance(tokens, int) and not isinstance(tokens, bool) else 0,
        tokens_per_second=quality._tokens_per_second(data),
        memory_gb=memory_gb,
    )
