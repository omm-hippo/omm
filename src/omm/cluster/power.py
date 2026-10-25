"""Keep cluster Macs from idle-sleeping.

Once a Mac idle-sleeps it only DarkWakes for ~45 s per network packet and
goes back to Maintenance Sleep, so the node keeps dropping out of the
cluster; setting sleep 0 later does not end that cycle, only a full wake
does (real Mac mini, `pmset -g log`, 2026-10-22). init/join set `sleep 0`
and remember the original value so `leave` can put it back.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from omm import config
from omm.cluster.errors import ClusterError

PMSET = "/usr/bin/pmset"
_SLEEP_LINE = re.compile(r"^\s*sleep\s+(\d+)\b", re.MULTILINE)


def saved_path() -> Path:
    return config.OMM_HOME / "cluster" / "sleep-before.txt"


def idle_sleep_minutes(text: str) -> int | None:
    """The in-use `sleep` value from `pmset -g` (0 = never)."""
    match = _SLEEP_LINE.search(text)
    return int(match.group(1)) if match else None


def current_idle_sleep_minutes(*, run=subprocess.run) -> int | None:
    result = run([PMSET, "-g"], capture_output=True, text=True, check=False)
    return idle_sleep_minutes(result.stdout or "")


def _set_sleep(minutes: int, run) -> bool:
    return run(["sudo", PMSET, "-a", "sleep", str(minutes)], check=False).returncode == 0


def keep_awake(*, run=subprocess.run) -> None:
    before = current_idle_sleep_minutes(run=run)
    path = saved_path()
    # A re-run of init/join must not overwrite the original with our own 0.
    if before is not None and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{before}\n", encoding="utf-8")
    if not _set_sleep(0, run):
        raise ClusterError("could not turn off idle sleep on this Mac (sudo pmset failed)")


def restore(*, run=subprocess.run) -> None:
    path = saved_path()
    try:
        before = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if not _set_sleep(before, run):
        raise ClusterError("could not restore this Mac's idle sleep setting (sudo pmset failed)")
    path.unlink(missing_ok=True)
