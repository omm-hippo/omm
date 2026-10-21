"""Per-user LaunchAgent that keeps the cluster gateway running on the head Mac.

Mirrors watch_service.py's launchd half: a named shim so Background Items does
not read "python", KeepAlive with a respawn floor, and bootout before every
bootstrap so a re-run `omm cluster init` never registers the job twice.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys

from omm import config

LAUNCHD_LABEL = "com.omm.cluster-gateway"
_SUBCOMMAND = "_cluster-gateway-run"


def service_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, _SUBCOMMAND]
    return [sys.executable, "-m", "omm.cli", _SUBCOMMAND]


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _wrapper_path() -> Path:
    return config.OMM_HOME / "bin" / "omm-cluster-gateway"


def _wrapper_content() -> str:
    argv = service_argv()[:-1]
    quoted = " ".join(f'"{part}"' for part in argv)
    return f'#!/bin/sh\nexec {quoted} "$@"\n'


def _plist_bytes() -> bytes:
    log_path = config.OMM_HOME / "logs" / "cluster-gateway.log"
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(_wrapper_path()), _SUBCOMMAND],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(*, run=subprocess.run) -> None:
    from omm.atomic import atomic_write_bytes, atomic_write_text

    wrapper = _wrapper_path()
    atomic_write_text(wrapper, _wrapper_content())
    wrapper.chmod(0o755)
    (config.OMM_HOME / "logs").mkdir(parents=True, exist_ok=True)
    plist = _plist_path()
    run(["launchctl", "bootout", f"{_domain()}/{LAUNCHD_LABEL}"], capture_output=True, text=True, check=False)
    atomic_write_bytes(plist, _plist_bytes())
    result = run(["launchctl", "bootstrap", _domain(), str(plist)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        from omm.cluster.errors import ClusterError

        raise ClusterError(f"launchctl could not start the gateway service: {(result.stderr or '').strip()}")


def uninstall(*, run=subprocess.run) -> None:
    run(["launchctl", "bootout", f"{_domain()}/{LAUNCHD_LABEL}"], capture_output=True, text=True, check=False)
    _plist_path().unlink(missing_ok=True)
    _wrapper_path().unlink(missing_ok=True)


def is_installed() -> bool:
    return _plist_path().is_file()


def is_running(*, run=subprocess.run) -> bool:
    result = run(["launchctl", "print", f"{_domain()}/{LAUNCHD_LABEL}"], capture_output=True, text=True, check=False)
    return result.returncode == 0 and "state = running" in (result.stdout or "")
