"""Per-user LaunchAgent that runs the staged exo backend on every cluster Mac.

The namespace keeps other exo installs on the same office network out of this
cluster. exo 1.0.71 reads EXO_LIBP2P_NAMESPACE while exo main reads
EXO_ZENOH_NAMESPACE, so both are set. exo shells out to networksetup,
system_profiler and scutil, which live in /usr/sbin; without them node
names, OS versions and Thunderbolt links stay empty.

Every start takes the next libp2p port in firewall.LIBP2P_PORTS. exo 1.0.71
makes a new peer id on every start, and the other Macs keep dialing the old id
at its old address and port every 5 s until their mDNS record of it expires
(2500 s). While the new exo listens on that same port, each of those dials
reaches it: the new exo counts a connection that the dialer drops at once on
the id mismatch, and every such connection starts an election that pauses the
whole cluster. With the port moved on, the stale dials are refused instead.
Real Mac minis 2026-11-06: a restart on the same port gave an election every
5 s for 34 min; on the next port, one election out and one back in.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from omm import config
from omm.cluster import firewall, launchd, preflight

LAUNCHD_LABEL = "com.omm.cluster-exo"

# Run by launchd with the port-index file as $1 and exo as $2; launchd's own
# restarts after a crash go through it too. exec keeps exo as the job's process.
# Without an index exo last ran on the first port (older omm), so start one past it.
LAUNCH_SCRIPT = f"""i=$(cat "$1" 2>/dev/null)
case "$i" in ''|*[!0-9]*) i=0 ;; esac
i=$(( (i + 1) % {len(firewall.LIBP2P_PORTS)} ))
echo "$i" > "$1"
exec "$2" --libp2p-port $(( {firewall.LIBP2P_PORTS.start} + i ))
"""


def _port_index_path() -> Path:
    return config.OMM_HOME / "cluster" / "exo-port-index"


def libp2p_port() -> int:
    """The libp2p port exo took at its last start (older omm always used the first)."""
    try:
        index = int(_port_index_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        index = 0
    ports = firewall.LIBP2P_PORTS
    return ports[index] if 0 <= index < len(ports) else ports[0]


def install(namespace: str, *, run=subprocess.run, keep_if_same: bool = False) -> None:
    runtime = str(preflight.runtime_dir())
    workdir = Path.home() / ".exo"
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = config.OMM_HOME / "logs" / "cluster-exo.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _port_index_path().parent.mkdir(parents=True, exist_ok=True)
    launchd.install_agent(LAUNCHD_LABEL, {
        "ProgramArguments": ["/bin/sh", "-c", LAUNCH_SCRIPT, "omm-cluster-exo",
                             str(_port_index_path()), f"{runtime}/exo"],
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
    }, run=run, keep_if_same=keep_if_same)


def uninstall(*, run=subprocess.run) -> None:
    launchd.uninstall_agent(LAUNCHD_LABEL, run=run)


def restart(*, run=subprocess.run) -> None:
    launchd.restart_agent(LAUNCHD_LABEL, run=run)


def is_running(*, run=subprocess.run) -> bool:
    return launchd.agent_running(LAUNCHD_LABEL, run=run)
