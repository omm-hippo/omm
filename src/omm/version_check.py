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

_TTL_SECONDS = 30 * 60
_CHECK_IN_FLIGHT_TTL_SECONDS = 60


def _cache_path() -> Path:
    return config.OMM_HOME / "update_check.json"


def _load() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _ref_entry(cache: dict, ref: str) -> dict:
    entry = cache.get(ref)
    return entry if isinstance(entry, dict) else {}


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
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < ttl_seconds:
        return entry.get("remote_head")
    latest = fetch(ref)
    cache[ref] = {"checked_at": time.time(), "remote_head": latest}
    _save(cache)
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
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < ttl_seconds:
        return True, entry.get("remote_head")
    return False, None


def should_start_check(ref: str = "main", ttl_seconds: int = _TTL_SECONDS) -> bool:
    """True if the cache is stale and no other recently-spawned detached
    child is already fetching (avoids piling up duplicate `git ls-remote`
    processes when several short `omm` commands run back to back)."""
    cache = _load()
    entry = _ref_entry(cache, ref)
    checked_at = entry.get("checked_at")
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < ttl_seconds:
        return False
    checking_since = entry.get("checking_since")
    if (
        isinstance(checking_since, (int, float))
        and time.time() - checking_since < _CHECK_IN_FLIGHT_TTL_SECONDS
    ):
        return False
    return True


def mark_checking(ref: str = "main") -> None:
    """Called right before spawning the detached child, so concurrent short
    `omm` invocations don't each spawn their own `git ls-remote` process."""
    cache = _load()
    entry = _ref_entry(cache, ref)
    entry["checking_since"] = time.time()
    cache[ref] = entry
    _save(cache)


def record(remote_head: str | None, ref: str = "main") -> None:
    """Overwrite the cache with a freshly-known remote head (e.g. right
    after `omm update` fetches it live), so the next background check
    doesn't serve a pre-update reading for up to `_TTL_SECONDS`."""
    cache = _load()
    cache[ref] = {"checked_at": time.time(), "remote_head": remote_head}
    _save(cache)
