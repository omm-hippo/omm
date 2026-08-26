"""Cross-run dedup record of models already benchmarked on this machine
(`~/.omm/benchmark_history.json`), used by `omm contribute` so it never
re-benchmarks the same model twice. Global, not TTY-scoped (unlike
session_cache.py) - benchmarking is a real, expensive action that should
stay deduped across every terminal on this machine, not just one session.
Best-effort: never raises out of this module.

The file holds two independent sections:

  "entries"  - successful, uploaded benchmarks. `loaded_refs()` and
               `has_been_benchmarked()` read only this one, so every
               success-only caller keeps its exact previous meaning.
  "failures" - attempts that produced no usable result. Kept separate on
               purpose: a failed attempt is not a benchmark, and folding it
               into "entries" would make every existing presence-of-key
               reader treat a failure as a success.

Old history files (no "failures" key) load as "no failures recorded", and
an older omm reading a newer file simply ignores the section it does not
know about, so both directions stay compatible.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omm import config
from omm.atomic import atomic_write_text, locked

# Failure reasons that describe *this machine right now* rather than the
# model: the memory guard cancelled the run, the runtime could not allocate,
# the model would not fit alongside whatever else was resident. Re-trying
# these costs a full re-download and, while the machine stays busy, produces
# the same cancellation - so they are the only reasons that build a cooldown
# streak. Everything else (timeouts, protocol errors, unknown) is recorded
# for diagnostics but never suppresses a candidate across runs.
MACHINE_FAILURE_REASONS = frozenset(
    {
        "memory_pressure_cancelled",
        "memory_pressure_unload_failed",
        "memory_guard_blocked",
        "memory_allocation_blocked",
        "memory_allocation_deferred",
        "out_of_memory",
    }
)
# Two consecutive machine-related failures, not one: a single cancellation
# can be caused by an unrelated app that has since closed. Two in a row on
# separate runs is a pattern.
MACHINE_FAILURE_COOLDOWN_STREAK = 2
# Time-boxed, never permanent. Free RAM changes; a candidate suppressed on a
# busy afternoon must become eligible again on its own.
MACHINE_FAILURE_COOLDOWN_HOURS = 24


def _path() -> Path:
    return config.OMM_HOME / "benchmark_history.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"entries": {}, "failures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": {}, "failures": {}}
    if not isinstance(data, dict):
        return {"entries": {}, "failures": {}}
    entries = data.get("entries")
    failures = data.get("failures")
    return {
        "entries": dict(entries) if isinstance(entries, dict) else {},
        "failures": dict(failures) if isinstance(failures, dict) else {},
    }


def _save(data: dict[str, Any]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data))
    except OSError:
        pass


def loaded_refs() -> set[str]:
    """Refs with a successful, uploaded benchmark. Failures are deliberately
    absent - a failed attempt must not read as "already benchmarked"."""
    return set(_load()["entries"].keys())


def has_been_benchmarked(ref: str) -> bool:
    return ref in _load()["entries"]


def is_machine_related_failure(reason: str | None) -> bool:
    return reason in MACHINE_FAILURE_REASONS


def record_benchmarked(
    ref: str, *, repo_id: str | None, filename: str, sha256: str, tokens_per_sec: float
) -> None:
    path = _path()
    with locked(path):
        data = _load()
        data["entries"][ref] = {
            "repo_id": repo_id,
            "filename": filename,
            "sha256": sha256,
            "tokens_per_sec": tokens_per_sec,
            "benchmarked_at": datetime.now(timezone.utc).isoformat(),
        }
        # A real result proves the earlier failures were circumstantial, so
        # the streak resets rather than lingering to suppress a model that
        # demonstrably works on this machine.
        data["failures"].pop(ref, None)
        _save(data)


def record_benchmark_failure(
    ref: str,
    *,
    repo_id: str | None,
    filename: str,
    reason: str | None,
    engine: str | None = None,
) -> None:
    """Record an attempt that produced no usable benchmark result.

    Machine-related reasons (see MACHINE_FAILURE_REASONS) accumulate a
    consecutive streak; any other reason records the attempt but leaves the
    streak untouched, so a one-off timeout can never push a candidate into
    the cooldown that low memory is meant to trigger. The cooldown clock is
    kept in its own field for the same reason: a later timeout must not
    silently extend a suppression window it did not earn.
    """
    path = _path()
    with locked(path):
        data = _load()
        now = datetime.now(timezone.utc).isoformat()
        previous = data["failures"].get(ref)
        if not isinstance(previous, dict):
            previous = {}
        machine_related = is_machine_related_failure(reason)
        streak = _streak_of(previous) + (1 if machine_related else 0)
        data["failures"][ref] = {
            "repo_id": repo_id,
            "filename": filename,
            "reason": reason,
            "outcome": "machine_failure" if machine_related else "other_failure",
            "engine": engine,
            "consecutive_machine_failures": streak,
            "first_failed_at": previous.get("first_failed_at") or now,
            "last_failed_at": now,
            "last_machine_failure_at": (
                now if machine_related else previous.get("last_machine_failure_at")
            ),
        }
        _save(data)


def failure_record(ref: str) -> dict[str, Any] | None:
    record = _load()["failures"].get(ref)
    return record if isinstance(record, dict) else None


def machine_failure_streak(ref: str) -> int:
    return _streak_of(failure_record(ref) or {})


def failure_cooldowns(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Refs currently suppressed by repeated machine-related failures, keyed
    by ref, with the stored failure record as the value.

    Suppression is time-boxed rather than conditional on a hardware
    re-measurement: the only condition that matters is free memory at the
    moment the benchmark runs, and that cannot be predicted from a snapshot
    taken while a different model is loading. Waiting out the cooldown is
    both cheaper and more honest than re-deriving it.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(hours=MACHINE_FAILURE_COOLDOWN_HOURS)
    cooling: dict[str, dict[str, Any]] = {}
    for ref, record in _load()["failures"].items():
        if not isinstance(record, dict):
            continue
        if _streak_of(record) < MACHINE_FAILURE_COOLDOWN_STREAK:
            continue
        last_failed = _parse_timestamp(record.get("last_machine_failure_at"))
        # An unreadable or missing timestamp must never suppress a candidate
        # forever; treat it as expired and let the next attempt rewrite it.
        if last_failed is None or last_failed <= cutoff:
            continue
        cooling[ref] = record
    return cooling


def cooldown_expires_at(record: dict[str, Any]) -> datetime | None:
    """When the cooldown on `record` lapses, or None if it cannot be dated."""
    last_failed = _parse_timestamp(record.get("last_machine_failure_at"))
    if last_failed is None:
        return None
    return last_failed + timedelta(hours=MACHINE_FAILURE_COOLDOWN_HOURS)


def _streak_of(record: dict[str, Any]) -> int:
    value = record.get("consecutive_machine_failures")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
