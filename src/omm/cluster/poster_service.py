"""Turn the poster art worker on and off on this cluster Mac.

The worker runs as per-user LaunchAgent com.omm.cluster-poster (the same shape
as member_service.py: a named shim so Background Items does not read "python",
KeepAlive, a respawn floor). `enable` stores the token in the keychain, the
public values in settings.json, builds the drawing venv and starts the agent;
the agent downloads the model itself before it takes any job. `disable` (also
part of `omm cluster leave`) removes all of that again.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import time

from omm import config
from omm.cluster import launchd, poster_config, poster_runtime
from omm.cluster.errors import ClusterError

LAUNCHD_LABEL = "com.omm.cluster-poster"
_SUBCOMMAND = "_cluster-poster-run"
NOT_IN_CLUSTER = ("This Mac is not in a cluster. Run omm cluster first; the poster worker runs only on cluster "
                  "Macs.")


def service_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, _SUBCOMMAND]
    return [sys.executable, "-m", "omm.cli", _SUBCOMMAND]


def _wrapper_path() -> Path:
    return config.OMM_HOME / "bin" / "omm-cluster-poster"


def _wrapper_content() -> str:
    argv = service_argv()[:-1]
    quoted = " ".join(f'"{part}"' for part in argv)
    return f'#!/bin/sh\nexec {quoted} "$@"\n'


def _job() -> dict:
    log_path = config.OMM_HOME / "logs" / "cluster-poster.log"
    return {
        "ProgramArguments": [str(_wrapper_path()), _SUBCOMMAND],
        "EnvironmentVariables": {
            "OMM_HOME": str(config.OMM_HOME),
            "PATH": ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin",
                                            "/sbin"])),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def install(*, run=subprocess.run) -> None:
    from omm.atomic import atomic_write_text

    wrapper = _wrapper_path()
    atomic_write_text(wrapper, _wrapper_content())
    wrapper.chmod(0o755)
    (config.OMM_HOME / "logs").mkdir(parents=True, exist_ok=True)
    launchd.install_agent(LAUNCHD_LABEL, _job(), run=run)


def uninstall(*, run=subprocess.run) -> None:
    launchd.uninstall_agent(LAUNCHD_LABEL, run=run)
    _wrapper_path().unlink(missing_ok=True)


def is_installed() -> bool:
    return launchd.plist_path(LAUNCHD_LABEL).is_file()


def is_running(*, run=subprocess.run) -> bool:
    return launchd.agent_running(LAUNCHD_LABEL, run=run)


def restart_if_installed(*, run=subprocess.run) -> None:
    """After omm itself changed: run the worker on the new code (a job in flight is re-queued by the site)."""
    if is_installed():
        launchd.restart_agent(LAUNCHD_LABEL, run=run)


def enable(env_text: str, out) -> None:
    from omm.cluster import commands, settings

    commands.require_macos()
    if settings.load() is None:
        raise ClusterError(NOT_IN_CLUSTER)
    poster_settings, token = poster_config.parse_env(env_text)
    # Settings first: they create the folder `disable` looks for, so a token stored
    # by a half-finished enable is still removed by disable.
    poster_config.save_settings(poster_settings)
    poster_config.store_token(token)
    if not poster_runtime.venv_ready():
        poster_runtime.build_venv(out=out)
    install()
    out("Poster art worker is on. It downloads the model (about 15 GB) in the background before taking jobs; "
        "omm cluster status shows progress.")


def disable() -> None:
    """Stop the worker and remove its token, settings, venv and model. Safe to repeat."""
    if is_installed():
        uninstall()
    if not poster_config.poster_dir().exists():
        return
    # Files before the token: over ssh the keychain is often locked, and the
    # ~15 GB of venv and model must go even then.
    poster_config.delete_settings()
    poster_runtime.remove_all()
    shutil.rmtree(poster_config.poster_dir(), ignore_errors=True)
    try:
        poster_config.delete_token()
    except ClusterError:
        # Recreate the folder so a re-run (after unlocking) still removes the token.
        poster_config.poster_dir().mkdir(parents=True, exist_ok=True)
        raise ClusterError("The poster worker is stopped and its files are removed, but its token is still in the "
                           "login keychain. Run `security unlock-keychain`, then run this again.") from None


def status() -> dict:
    from omm.cluster import poster_worker

    installed = is_installed()
    running = installed and is_running()
    state = poster_worker.read_state() if installed else None
    return {"installed": installed, "running": running, "state": state,
            "line": poster_worker.status_line(state, installed, running, time.time())}
