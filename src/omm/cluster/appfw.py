"""macOS Application Firewall exceptions for the gateway and exo.

With the Application Firewall on, an inbound connection to a binary it does
not know is held until someone clicks Allow on the Mac's screen; a headless
head Mac's gateway accepted TCP but never answered (node3, 2026-10-22).
init/join add the binaries that must accept connections, record the ones omm
added, and leave removes only those. ALF keys a signed binary by its code
signature, so a staged exo copy merges into any entry EXO.app already has and
never shows up under its own path: only entries that appear under the added
path are recorded, and only path-keyed entries can be checked. pf
(firewall.py) still decides which ports the office network can reach.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Iterable

from omm import config
from omm.cluster import preflight
from omm.cluster.errors import ClusterError

SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def record_path() -> Path:
    return config.OMM_HOME / "cluster" / "appfw-added.txt"


def gateway_binary() -> Path:
    """The executable the gateway process really runs as (what ALF sees)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    # Framework builds (Homebrew, python.org) re-exec into this app binary.
    app = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    if app.is_file():
        return app.resolve()
    return Path(getattr(sys, "_base_executable", sys.executable)).resolve()


def exo_binary() -> Path:
    return preflight.runtime_dir() / "exo"


def parse_listapps(text: str) -> dict[str, bool]:
    """`--listapps` output as {path: incoming allowed}."""
    apps: dict[str, bool] = {}
    path = None
    for line in text.splitlines():
        head, sep, rest = line.partition(" : ")
        if sep and head.strip().isdigit():
            path = rest.strip()
        elif path is not None and "incoming connections" in line:
            apps[path] = "Allow" in line
            path = None
    return apps


def _listed(run) -> dict[str, bool]:
    result = run([SOCKETFILTERFW, "--listapps"], capture_output=True, text=True, check=False)
    return parse_listapps(result.stdout or "")


def _recorded() -> list[str]:
    try:
        return [line for line in record_path().read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []


def allow(binaries: Iterable[Path], *, run=subprocess.run) -> None:
    before = _listed(run)
    pending = [str(binary) for binary in binaries if not before.get(str(binary))]
    for binary in pending:
        for flag in ("--add", "--unblockapp"):
            if run(["sudo", SOCKETFILTERFW, flag, binary], check=False).returncode != 0:
                raise ClusterError(f"could not let {binary} through the macOS firewall (sudo failed)")
    if not pending:
        return
    after = _listed(run)
    recorded = _recorded()
    recorded += [b for b in pending if b in after and b not in before and b not in recorded]
    if recorded:
        record_path().parent.mkdir(parents=True, exist_ok=True)
        record_path().write_text("".join(f"{path}\n" for path in recorded), encoding="utf-8")


def remove(*, run=subprocess.run) -> None:
    recorded = _recorded()
    for binary in recorded:
        if run(["sudo", SOCKETFILTERFW, "--remove", binary], check=False).returncode != 0:
            raise ClusterError(f"could not remove {binary} from the macOS firewall (sudo failed)")
    record_path().unlink(missing_ok=True)


def not_allowed(binaries: Iterable[Path], *, run=subprocess.run) -> list[str]:
    state = run([SOCKETFILTERFW, "--getglobalstate"], capture_output=True, text=True, check=False)
    if "enabled" not in (state.stdout or ""):
        return []
    listed = _listed(run)
    return [str(binary) for binary in binaries if not listed.get(str(binary))]
