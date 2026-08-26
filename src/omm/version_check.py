"""Best-effort, TTL-cached remote-HEAD lookup backing the background
"update available" notice (see cli.py's `_maybe_start_update_check`).
Never raises; a cache miss/failure just means the next `omm` invocation
tries the network fetch again once the TTL expires.

Most `omm` commands finish faster than a `git ls-remote` round trip, so
the actual fetch runs in a detached child process (spawned by cli.py,
independent of the parent's lifetime) that writes its result here once
done. The version check is therefore spread across several short `omm`
invocations: one kicks off the fetch, a later one sees the fresh cache
and shows the notice."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from omm import config
from omm.atomic import atomic_write_text, locked

_TTL_SECONDS = 30 * 60
_CHECK_IN_FLIGHT_TTL_SECONDS = 60


def _cache_path() -> Path:
    return config.OMM_HOME / "update_check.json"


def _load() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(data: dict) -> bool:
    try:
        path = _cache_path()
        atomic_write_text(path, json.dumps(data))
    except (OSError, TypeError):
        return False
    return True


def _ref_entry(cache: dict, ref: str) -> dict:
    entry = cache.get(ref)
    return entry if isinstance(entry, dict) else {}


def _fresh(timestamp: object, ttl_seconds: int | float) -> bool:
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not isinstance(ttl_seconds, (int, float))
        or isinstance(ttl_seconds, bool)
        or ttl_seconds <= 0
    ):
        return False
    age = time.time() - timestamp
    return 0 <= age < ttl_seconds


def _remote_head(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def cached_remote_head(
    fetch: Callable[[str], str | None],
    ref: str = "main",
    ttl_seconds: int = _TTL_SECONDS,
) -> str | None:
    """`fetch` is injected (cli._remote_head_commit) so the actual
    `git ls-remote` call stays single-sourced in cli.py. A `None` result
    (offline/unreachable) is cached too, same TTL, so an offline run
    doesn't retry the network call on every command.

    Cached per `ref` (branch), so switching update channel (stable/beta)
    never serves a stale reading recorded for the other branch."""
    cache = _load()
    entry = _ref_entry(cache, ref)
    checked_at = entry.get("checked_at")
    if _fresh(checked_at, ttl_seconds):
        return _remote_head(entry.get("remote_head"))
    try:
        latest = _remote_head(fetch(ref))
    except Exception:
        latest = None
    # Merge into the latest cache under the writer lock. Another detached
    # checker may have updated a different channel while this fetch ran.
    try:
        with locked(_cache_path()):
            cache = _load()
            cache[ref] = {"checked_at": time.time(), "remote_head": latest}
            _save(cache)
    except (OSError, TimeoutError):
        pass
    return latest


def cached_remote_head_if_fresh(
    ref: str = "main", ttl_seconds: int = _TTL_SECONDS
) -> tuple[bool, str | None]:
    """Non-blocking read: never fetches. Returns `(True, remote_head)` if a
    prior check (this run or a detached child from an earlier one) is still
    within TTL, else `(False, None)` meaning the caller should decide
    whether to kick off a fresh check itself."""
    cache = _load()
    entry = _ref_entry(cache, ref)
    checked_at = entry.get("checked_at")
    if _fresh(checked_at, ttl_seconds):
        return True, _remote_head(entry.get("remote_head"))
    return False, None


def should_start_check(ref: str = "main", ttl_seconds: int = _TTL_SECONDS) -> bool:
    """True if the cache is stale and no other recently-spawned detached
    child is already fetching (avoids piling up duplicate `git ls-remote`
    processes when several short `omm` commands run back to back)."""
    cache = _load()
    entry = _ref_entry(cache, ref)
    checked_at = entry.get("checked_at")
    if _fresh(checked_at, ttl_seconds):
        return False
    checking_since = entry.get("checking_since")
    if _fresh(checking_since, _CHECK_IN_FLIGHT_TTL_SECONDS):
        return False
    return True


def mark_checking(
    ref: str = "main", ttl_seconds: int = _TTL_SECONDS
) -> bool:
    """Atomically claim the right to spawn a detached checker.

    ``should_start_check`` is a lock-free hint. Two CLI processes can both see
    it as true, so this writer re-checks both freshness markers under the file
    lock before recording its claim. Only the process receiving ``True`` may
    spawn a child.
    """
    try:
        with locked(_cache_path()):
            cache = _load()
            entry = _ref_entry(cache, ref)
            if _fresh(entry.get("checked_at"), ttl_seconds) or _fresh(
                entry.get("checking_since"), _CHECK_IN_FLIGHT_TTL_SECONDS
            ):
                return False
            entry["checking_since"] = time.time()
            cache[ref] = entry
            if not _save(cache):
                return False
    except (OSError, TimeoutError):
        return False
    return True


def record(remote_head: str | None, ref: str = "main") -> None:
    """Overwrite the cache with a freshly-known remote head (e.g. right
    after `omm update` fetches it live), so the next background check
    doesn't serve a pre-update reading for up to `_TTL_SECONDS`."""
    try:
        with locked(_cache_path()):
            cache = _load()
            cache[ref] = {
                "checked_at": time.time(),
                "remote_head": _remote_head(remote_head),
            }
            _save(cache)
    except (OSError, TimeoutError):
        pass
