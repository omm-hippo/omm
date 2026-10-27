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


def install_agent(label: str, job: dict, *, run=subprocess.run, sleep=time.sleep, keep_if_same: bool = False) -> None:
    """Write the plist and (re)start the job. `keep_if_same` leaves a loaded job with the same plist running."""
    from omm.atomic import atomic_write_bytes

    path = plist_path(label)
    content = plistlib.dumps({"Label": label, **job})
    if keep_if_same and path.is_file() and path.read_bytes() == content and run(
            ["launchctl", "print", f"{domain()}/{label}"], capture_output=True, text=True, check=False).returncode == 0:
        return
    run(["launchctl", "bootout", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    atomic_write_bytes(path, content)
    for attempt in range(_BOOTSTRAP_ATTEMPTS):
        result = run(["launchctl", "bootstrap", domain(), str(path)], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        if attempt + 1 < _BOOTSTRAP_ATTEMPTS:
            sleep(1)
    raise ClusterError(f"launchctl could not start {label}: {(result.stderr or '').strip()}")


def uninstall_agent(label: str, *, run=subprocess.run) -> None:
    # The plist goes first: a job that boots itself out (a head stepping down)
    # is killed by bootout, and a plist left behind starts it again at login.
    plist_path(label).unlink(missing_ok=True)
    run(["launchctl", "bootout", f"{domain()}/{label}"], capture_output=True, text=True, check=False)


def agent_running(label: str, *, run=subprocess.run) -> bool:
    result = run(["launchctl", "print", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    return result.returncode == 0 and "state = running" in (result.stdout or "")


def restart_agent(label: str, *, run=subprocess.run) -> None:
    """Kill and relaunch an installed job in place (`kickstart -k`), keeping its plist."""
    result = run(["launchctl", "kickstart", "-k", f"{domain()}/{label}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ClusterError(f"launchctl could not restart {label}: {(result.stderr or '').strip()}")
