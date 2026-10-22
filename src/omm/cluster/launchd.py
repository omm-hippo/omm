"""Shared per-user LaunchAgent plumbing for the cluster services."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import time

from omm.cluster.errors import ClusterError

# `launchctl bootout` returns before the job is gone; an immediate bootstrap
# then fails with "5: Input/output error" (seen on a real Mac mini).
_BOOTSTRAP_ATTEMPTS = 5


def domain() -> str:
    return f"gui/{os.getuid()}"


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def install_agent(label: str, job: dict, *, run=subprocess.run, sleep=time.sleep) -> None:
    from omm.atomic import atomic_write_bytes

    path = plist_path(label)
    run(["launchctl", "bootout", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    atomic_write_bytes(path, plistlib.dumps({"Label": label, **job}))
    for attempt in range(_BOOTSTRAP_ATTEMPTS):
        result = run(["launchctl", "bootstrap", domain(), str(path)], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        if attempt + 1 < _BOOTSTRAP_ATTEMPTS:
            sleep(1)
    raise ClusterError(f"launchctl could not start {label}: {(result.stderr or '').strip()}")


def uninstall_agent(label: str, *, run=subprocess.run) -> None:
    run(["launchctl", "bootout", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    plist_path(label).unlink(missing_ok=True)


def agent_running(label: str, *, run=subprocess.run) -> bool:
    result = run(["launchctl", "print", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    return result.returncode == 0 and "state = running" in (result.stdout or "")
