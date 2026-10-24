"""Per-user LaunchAgent that runs the staged exo backend on every cluster Mac.

The namespace keeps other exo installs on the same office network out of this
cluster. exo 1.0.71 reads EXO_LIBP2P_NAMESPACE while exo main reads
EXO_ZENOH_NAMESPACE, so both are set. exo shells out to networksetup,
system_profiler and scutil, which live in /usr/sbin; without them node
names, OS versions and Thunderbolt links stay empty.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from omm import config
from omm.cluster import launchd, preflight

LAUNCHD_LABEL = "com.omm.cluster-exo"
LIBP2P_PORT = 52416


def install(namespace: str, *, run=subprocess.run) -> None:
    runtime = str(preflight.runtime_dir())
    workdir = Path.home() / ".exo"
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = config.OMM_HOME / "logs" / "cluster-exo.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launchd.install_agent(LAUNCHD_LABEL, {
        "ProgramArguments": [f"{runtime}/exo", "--libp2p-port", str(LIBP2P_PORT)],
        "WorkingDirectory": str(workdir),
        "EnvironmentVariables": {
            "EXO_RUNTIME_DIR": runtime,
            "EXO_LIBP2P_NAMESPACE": namespace,
            "EXO_ZENOH_NAMESPACE": namespace,
            "PATH": ":".join([runtime, f"{runtime}/_internal", "/opt/homebrew/bin", "/usr/local/bin",
                              "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }, run=run)


def uninstall(*, run=subprocess.run) -> None:
    launchd.uninstall_agent(LAUNCHD_LABEL, run=run)


def restart(*, run=subprocess.run) -> None:
    launchd.restart_agent(LAUNCHD_LABEL, run=run)


def is_running(*, run=subprocess.run) -> bool:
    return launchd.agent_running(LAUNCHD_LABEL, run=run)
