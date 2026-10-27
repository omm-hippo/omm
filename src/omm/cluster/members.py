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
        rows[node_id] = {**report, "seen_at": now.isoformat()}
        atomic_write_text(_members_path(), json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def clear() -> None:
    _members_path().unlink(missing_ok=True)


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
