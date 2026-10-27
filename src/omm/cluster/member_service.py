"""Per-user LaunchAgent that keeps the member link running on every member Mac.

Mirrors watch_service.py's launchd half: a named shim so Background Items does
not read "python", KeepAlive with a respawn floor, and bootout before every
bootstrap so a re-run `omm cluster join` never registers the job twice.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from omm import config
from omm.cluster import launchd

LAUNCHD_LABEL = "com.omm.cluster-member"
_SUBCOMMAND = "_cluster-member-run"


def service_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, _SUBCOMMAND]
    return [sys.executable, "-m", "omm.cli", _SUBCOMMAND]


def _wrapper_path() -> Path:
    return config.OMM_HOME / "bin" / "omm-cluster-member"


def _wrapper_content() -> str:
    argv = service_argv()[:-1]
    quoted = " ".join(f'"{part}"' for part in argv)
    return f'#!/bin/sh\nexec {quoted} "$@"\n'


def install(*, run=subprocess.run) -> None:
    from omm.atomic import atomic_write_text

    wrapper = _wrapper_path()
    atomic_write_text(wrapper, _wrapper_content())
    wrapper.chmod(0o755)
    log_path = config.OMM_HOME / "logs" / "cluster-member.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launchd.install_agent(LAUNCHD_LABEL, {
        "ProgramArguments": [str(wrapper), _SUBCOMMAND],
        # launchd starts jobs with a bare environment; without this a custom
        # OMM_HOME would make the link read another cluster.json.
        "EnvironmentVariables": {"OMM_HOME": str(config.OMM_HOME)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }, run=run)


def uninstall(*, run=subprocess.run) -> None:
    launchd.uninstall_agent(LAUNCHD_LABEL, run=run)
    _wrapper_path().unlink(missing_ok=True)


def is_installed() -> bool:
    return launchd.plist_path(LAUNCHD_LABEL).is_file()


def is_running(*, run=subprocess.run) -> bool:
    return launchd.agent_running(LAUNCHD_LABEL, run=run)
