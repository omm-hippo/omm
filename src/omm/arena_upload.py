"""Arena battle vote uploads (`omm arena`'s opt-in outbound channel).

Sub-project B of the arena roadmap: take the rows `omm arena` wrote to
``~/.omm/arena/votes.jsonl`` and ship them, opt-in, through the shared
Cloudflare Worker PoW gateway into the private Firebase RTDB ``votes``
node. Modeled on ``usage.py``, which solved the same queue/backoff/flush
problems for the anonymous daily batch.

**The user's prompt text is never uploaded**, and neither is anything
derived from it. ``build_payload`` allow-lists what reaches the wire; the
Worker's ``validateVoteEvent`` rejects a ``prompt`` key by name as a second
line of defense. See
docs/superpowers/specs/2026-09-26-arena-vote-upload-channel-design.md and
the user-facing PRIVACY.md.

Every public function here swallows its own errors: a vote is already saved
locally by the time any of this runs, and an upload problem must never take
down the battle session that produced it.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from filelock import Timeout as FileLockTimeout

from . import config
from .atomic import atomic_write_text, locked

logger = logging.getLogger("omm.arena_upload")

SCHEMA_VERSION = 1
_PENDING_MAX = 5000
_MAX_LOG_LINES = 500
_DETAIL_SLICE = 300
_BACKOFF_SECONDS = 6 * 3600

WINNERS = frozenset({"a", "b", "both_bad"})
ENGINES = frozenset({"ollama", "lmstudio"})

#: Optional measurements that must be present on both sides or neither: the
#: Worker validator rejects an asymmetric row, because sub-project C compares
#: the two sides of one battle and half a measurement is not comparable.
_SYMMETRIC_OPTIONAL = ("memory_gb", "tokens_per_second")

#: Every key that may appear on the wire. Mirrored by `VOTE_FIELDS` in
#: cf-worker/src/validate.ts and by the `votes` block in
#: database.rules.json - all three must agree.
WIRE_FIELDS = frozenset(
    {
        "schema_version",
        "battle_id",
        "client_id",
        "client_version",
        "recorded_at",
        "engine",
        "winner",
        "pinned",
    }
    | {
        f"{name}_{side}"
        for side in ("a", "b")
        for name in (
            "model_provider",
            "model_repo_id",
            "model_filename",
            "model_digest",
            "quant_bits",
            "elapsed",
            "tokens",
            "tokens_per_second",
            "memory_gb",
            "watt",
        )
    }
)


def arena_dir() -> Path:
    """Resolved at call time; OMM_HOME is overridable (runlog._logs_dir)."""
    return config.OMM_HOME / "arena"


def _pending_path() -> Path:
    return arena_dir() / "votes-pending.jsonl"


def _backoff_path() -> Path:
    return arena_dir() / "votes-backoff.json"


def _log_path() -> Path:
    return arena_dir() / "votes-upload.log"


def policy(config_data: dict[str, Any] | None = None) -> str:
    """"always" | "never" | "ask" - the same vocabulary
    telemetry_send_policy uses, because the consent prompt is the same
    y/n/a whose "a" saves "always"."""
    data = config_data if config_data is not None else config.load_config()
    value = data.get("arena_vote_send_policy")
    return value if value in {"always", "never", "ask"} else "ask"


def _client_version() -> str | None:
    try:
        from . import package_metadata

        version = package_metadata.version()
    except Exception:
        return None
    return version if isinstance(version, str) and version else None


def _optional_str(value: object, limit: int) -> str | None:
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().removeprefix("sha256:")
    if len(lowered) == 64 and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    return None


def build_payload(vote_row: dict, *, registry_entries: dict | None = None) -> dict | None:
    """One wire payload from one local votes.jsonl row, or None when the row
    cannot make a valid one.

    Allow-listing, not copy-then-delete: sub-project A's local row gains
    fields over time (it gained tokens_per_second_a/b during its own
    implementation), and a builder that copied the row would forward each
    new field to a validator that rejects unknown keys.
    """
    if not isinstance(vote_row, dict):
        return None
    battle_id = _optional_str(vote_row.get("battle_id"), 64)
    recorded_at = _optional_str(vote_row.get("timestamp"), 50)
    winner = vote_row.get("winner")
    engine_a = vote_row.get("engine_a")
    if (
        battle_id is None
        or recorded_at is None
        or winner not in WINNERS
        or engine_a not in ENGINES
        # A session is single-engine; one `engine` field cannot express a
        # cross-engine row, so refuse rather than pick a side.
        or vote_row.get("engine_b") != engine_a
    ):
        return None

    if registry_entries is None:
        from . import registry

        try:
            registry_entries = registry.load_registry()
        except Exception:
            registry_entries = {}

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "battle_id": battle_id,
        "client_id": config.client_id(),
        "recorded_at": recorded_at,
        "engine": engine_a,
        "winner": winner,
        "pinned": bool(vote_row.get("pinned")),
    }
    client_version = _client_version()
    if client_version is not None:
        payload["client_version"] = client_version

    # Required per side. A row missing either side's basics is unusable.
    sides: dict[str, dict[str, Any]] = {}
    for side in ("a", "b"):
        filename = _optional_str(vote_row.get(f"model_{side}"), 300)
        elapsed = _optional_number(vote_row.get(f"elapsed_{side}"))
        tokens = vote_row.get(f"tokens_{side}")
        if (
            filename is None
            or elapsed is None
            or isinstance(tokens, bool)
            or not isinstance(tokens, int)
        ):
            return None
        sides[side] = {"filename": filename, "elapsed": elapsed, "tokens": tokens}

    for side, values in sides.items():
        payload[f"model_filename_{side}"] = values["filename"]
        payload[f"elapsed_{side}"] = round(values["elapsed"], 3)
        payload[f"tokens_{side}"] = values["tokens"]
        entry = registry_entries.get(values["filename"]) or {}
        if not isinstance(entry, dict):
            entry = {}
        provider = _optional_str(entry.get("provider"), 64)
        if provider is not None:
            payload[f"model_provider_{side}"] = provider
        repo_id = _optional_str(entry.get("repo_id"), 512)
        if repo_id is not None:
            payload[f"model_repo_id_{side}"] = repo_id
        digest = _digest(entry.get("sha256"))
        if digest is not None:
            payload[f"model_digest_{side}"] = digest
        quant_bits = _optional_number(entry.get("quantization_bits"))
        if quant_bits is not None:
            payload[f"quant_bits_{side}"] = quant_bits

    # Identity fields (provider/repo_id/digest/quant_bits) are independently
    # optional per side and may legitimately be asymmetric: one model can come
    # from a provider that gave a sha256 while the other was adopted from a
    # local directory with none. Only the *measurements* below require
    # symmetry, because those are what sub-project C compares side to side.
    for name in _SYMMETRIC_OPTIONAL:
        left = _optional_number(vote_row.get(f"{name}_a"))
        right = _optional_number(vote_row.get(f"{name}_b"))
        if left is not None and right is not None:
            payload[f"{name}_a"] = left
            payload[f"{name}_b"] = right

    # watt_* is never sent by sub-project B - no power reader exists yet. The
    # validator and rules accept the field so sub-project C, its first
    # consumer, needs no rules redeploy.
    return payload
