"""Tracks whether `omm contribute` has already covered every currently
published candidate for this hardware (`~/.omm/contribute_state.json`), so
the next run can warn up front instead of the user re-running a session
that has nothing new to try. Best-effort: never raises out of this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omm import config
from omm.atomic import atomic_write_text, locked


def _path() -> Path:
    return config.OMM_HOME / "contribute_state.json"


def load() -> dict[str, Any] | None:
    path = _path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    total = state.get("total_candidates")
    covered = state.get("covered_candidates")
    exhausted_at = state.get("exhausted_at")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(covered, bool)
        or not isinstance(covered, int)
        or not 0 <= covered <= total
        or not isinstance(exhausted_at, str)
        or not exhausted_at.strip()
    ):
        return None
    return state


def record_exhausted(total_candidates: int, covered_candidates: int) -> None:
    if (
        isinstance(total_candidates, bool)
        or not isinstance(total_candidates, int)
        or total_candidates < 0
        or isinstance(covered_candidates, bool)
        or not isinstance(covered_candidates, int)
        or not 0 <= covered_candidates <= total_candidates
    ):
        return
    path = _path()
    try:
        with locked(path):
            atomic_write_text(
                path,
                json.dumps(
                    {
                        "total_candidates": total_candidates,
                        "covered_candidates": covered_candidates,
                        "exhausted_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
    except OSError:
        pass
