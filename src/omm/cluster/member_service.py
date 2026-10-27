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


def _job() -> dict:
    log_path = config.OMM_HOME / "logs" / "cluster-member.log"
    return {
        "ProgramArguments": [str(_wrapper_path()), _SUBCOMMAND],
        # launchd starts jobs with a bare environment; without this a custom
        # OMM_HOME would make the link read another cluster.json, and following
        # the head's version could not find pipx (install.sh puts it in ~/.local/bin).
        "EnvironmentVariables": {
            "OMM_HOME": str(config.OMM_HOME),
            "PATH": ":".join(dict.fromkeys([str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin"),
                                            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
                                            "/sbin"])),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def _write_wrapper() -> None:
    from omm.atomic import atomic_write_text

    wrapper = _wrapper_path()
    atomic_write_text(wrapper, _wrapper_content())
    wrapper.chmod(0o755)
    (config.OMM_HOME / "logs").mkdir(parents=True, exist_ok=True)


def install(*, run=subprocess.run) -> None:
    _write_wrapper()
    launchd.install_agent(LAUNCHD_LABEL, _job(), run=run)


def _spawn_detached(argv: list[str]) -> None:
    # Its own session, so it outlives this job when bootout stops it.
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def refresh(*, spawn=_spawn_detached) -> bool:
    """From inside the running link: when its plist is out of date, rewrite it and have launchd reload it.

    launchd reads a plist only at bootstrap, and a job cannot bootstrap itself
    after bootout has stopped it, so a detached shell does both. True when it was started.
    """
    import plistlib

    from omm.atomic import atomic_write_bytes

    path = launchd.plist_path(LAUNCHD_LABEL)
    wanted = plistlib.dumps({"Label": LAUNCHD_LABEL, **_job()})
    if path.is_file() and path.read_bytes() == wanted:
        return False
    _write_wrapper()
    atomic_write_bytes(path, wanted)
    target = f"{launchd.domain()}/{LAUNCHD_LABEL}"
    spawn(["/bin/sh", "-c", f"sleep 2; launchctl bootout {target}; launchctl bootstrap {launchd.domain()} '{path}'"])
    return True


def uninstall(*, run=subprocess.run) -> None:
    launchd.uninstall_agent(LAUNCHD_LABEL, run=run)
    _wrapper_path().unlink(missing_ok=True)


def is_installed() -> bool:
    return launchd.plist_path(LAUNCHD_LABEL).is_file()


def is_running(*, run=subprocess.run) -> bool:
    return launchd.agent_running(LAUNCHD_LABEL, run=run)
