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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_exhausted(total_candidates: int, covered_candidates: int) -> None:
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
