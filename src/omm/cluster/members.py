"""What the head knows about its members: their last report, and a pending handover.

Both live under OMM_HOME/cluster on the head only. Reports come from each
member's link (member_link.py) on every poll.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from omm import config

HANDOVER_SECONDS = 300


def _members_path() -> Path:
    return config.OMM_HOME / "cluster" / "members.json"


def _handover_path() -> Path:
    return config.OMM_HOME / "cluster" / "handover.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load() -> dict[str, dict]:
    return {node: row for node, row in _read(_members_path()).items() if isinstance(row, dict)}


def record(node_id: str, report: dict, *, now: datetime) -> None:
    from omm.atomic import atomic_write_text, locked

    with locked(_members_path()):
        rows = load()
        row = {**report, "seen_at": now.isoformat()}
        rate = _event_rate(rows.get(node_id), report, now)
        if rate is not None:
            row["event_rate"] = rate
        rows[node_id] = row
        atomic_write_text(_members_path(), json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def _event_rate(previous: dict | None, report: dict, now: datetime) -> float | None:
    """Events per second a catching-up member replayed since its last report, for the head's ETA."""
    index = report.get("event_index")
    if not report.get("catching_up") or not isinstance(index, int) or not isinstance(previous, dict):
        return None
    before = previous.get("event_index")
    try:
        seconds = (now - datetime.fromisoformat(previous["seen_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(before, int) or index <= before or seconds <= 0:
        return previous.get("event_rate") if index == before else None
    return (index - before) / seconds


def _silent_path() -> Path:
    return config.OMM_HOME / "cluster" / "silent-since.json"


def silent_since(node_ids, *, now: datetime) -> dict[str, datetime]:
    """When status first saw each of these Macs in exo without a recent check-in.

    A Mac that exo only just found has had no chance to check in yet (its link
    polls every minute), so status gives it a grace period before advising a
    re-join. Macs not passed in are forgotten, so the clock starts again the
    next time one goes silent.
    """
    from omm.atomic import atomic_write_text, locked

    wanted = set(node_ids)
    if not wanted and not _silent_path().exists():
        return {}
    with locked(_silent_path()):
        stored = _read(_silent_path())
        since: dict[str, datetime] = {}
        for node_id in wanted:
            try:
                since[node_id] = datetime.fromisoformat(stored[node_id])
            except (KeyError, TypeError, ValueError):
                since[node_id] = now
        rows = {node_id: when.isoformat() for node_id, when in since.items()}
        if rows != stored:
            if rows:
                atomic_write_text(_silent_path(), json.dumps(rows, indent=2) + "\n")
            else:
                _silent_path().unlink(missing_ok=True)
    return since


def clear() -> None:
    _members_path().unlink(missing_ok=True)
    _silent_path().unlink(missing_ok=True)


def start_handover(to: str, epoch: int, settings: dict, rag_marker: str | None, *, now: datetime) -> None:
    from omm.atomic import atomic_write_text

    atomic_write_text(_handover_path(), json.dumps({
        "to": to, "epoch": epoch, "expires_at": (now + timedelta(seconds=HANDOVER_SECONDS)).isoformat(),
        "settings": settings, "rag_marker": rag_marker}, ensure_ascii=False, indent=2) + "\n")


def pending_handover(*, now: datetime | None = None) -> dict | None:
    data = _read(_handover_path())
    try:
        expires = datetime.fromisoformat(data["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if (now or datetime.now(timezone.utc)) >= expires:
        return None
    return data


def clear_handover() -> None:
    _handover_path().unlink(missing_ok=True)
