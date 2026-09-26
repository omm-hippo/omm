"""Local blind model battles (`omm arena`).

Sub-project A of the arena roadmap: pairing, blind generation, vote capture,
and a local-only vote log. Nothing here uploads anything; `votes.jsonl` is
read by sub-project B when that lands, never by this module.

Design: docs/superpowers/specs/2026-09-25-arena-battle-cli-design.md
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import Timeout as FileLockTimeout

from . import config
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
