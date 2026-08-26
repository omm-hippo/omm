"""Per-TTY session cache for `omm`: lets `search`/`list`/`recommend` results
be referenced later by number and pulled into Tab-completion, without any
in-memory state - Tab-completion runs as a fresh process on every keypress,
so state has to survive on disk. Best-effort only: never raises out of this
module. TTY-scoped so two terminal windows never see each other's results.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path

from omm import config
from omm.atomic import atomic_write_text, locked

_MAX_SEEN = 50


def _session_path() -> Path | None:
    # Use the OS-level stdin fd (always 0 on POSIX) rather than
    # sys.stdin.fileno() - test runners and other harnesses often swap
    # sys.stdin for an object whose .fileno() raises before os.ttyname()
    # ever runs, which would short-circuit this to "no session" even when
    # fd 0 itself is a real tty.
    try:
        session_key = os.ttyname(0)
    except OSError:
        # Not a real tty (piped input, CI, non-interactive) - no session.
        return None
    except AttributeError:
        # os.ttyname doesn't exist on Windows at all. Fall back to the
        # console window handle: pipx (and pip's other console-script
        # shims) install `omm` as an .exe launcher stub that spawns
        # python.exe as a *new child process* per invocation - Windows has
        # no exec() to replace the process image in place - so the running
        # process's direct parent is that ephemeral stub, not the
        # persistent shell, and getppid() changes on every single call.
        # GetConsoleWindow() instead returns the HWND of the console window
        # the calling process is attached to, which stays constant across
        # that whole stub -> python.exe hop (child processes inherit the
        # console unless a step explicitly detaches), so it plays the same
        # role ttyname() plays on POSIX: same window -> same key, different
        # windows -> different keys.
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            hwnd = 0
        if hwnd:
            session_key = str(hwnd)
        else:
            # No console attached (e.g. detached/GUI process) - fall back
            # to the parent pid, best-effort.
            try:
                session_key = str(os.getppid())
            except OSError:
                return None
    digest = hashlib.sha1(session_key.encode()).hexdigest()
    return config.OMM_HOME / "session" / f"{digest}.json"


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _load(path: Path | None = None) -> dict[str, list[str]]:
    path = path or _session_path()
    if path is None or not path.exists():
        return {"seen": [], "last_results": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": [], "last_results": []}
    if not isinstance(data, dict):
        return {"seen": [], "last_results": []}
    return {
        "seen": _string_list(data.get("seen")),
        "last_results": _string_list(data.get("last_results")),
    }


def _save(data: dict[str, list[str]], path: Path | None = None) -> None:
    path = path or _session_path()
    if path is None:
        return
    try:
        atomic_write_text(path, json.dumps(data))
    except OSError:
        pass


def record_seen(refs: list[str]) -> None:
    if not refs:
        return
    path = _session_path()
    if path is None:
        return
    try:
        with locked(path):
            data = _load(path)
            merged = list(refs) + [r for r in data["seen"] if r not in refs]
            data["seen"] = merged[:_MAX_SEEN]
            _save(data, path)
    except OSError:
        pass


def record_results(refs: list[str]) -> None:
    path = _session_path()
    if path is None:
        return
    try:
        with locked(path):
            data = _load(path)
            data["last_results"] = list(refs)
            merged = list(refs) + [r for r in data["seen"] if r not in refs]
            data["seen"] = merged[:_MAX_SEEN]
            _save(data, path)
    except OSError:
        pass


def load_seen() -> list[str]:
    return _load()["seen"]


def load_last_results() -> list[str]:
    return _load()["last_results"]
