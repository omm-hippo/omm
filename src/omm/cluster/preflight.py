"""Checks and installs every cluster Mac needs before exo can run.

exo's own app waits for on-screen approval the first time it starts, and a
backend exec'd from inside the bundle stalls launched-suspended until that
happens, so omm runs a copy of the bundled runtime from OMM_HOME instead
(spike findings, 2026-10-22).
"""

from __future__ import annotations

from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
from typing import Callable, Literal

from omm import config
from omm.cluster.errors import ClusterError

__all__ = ["ClusterError"]

MIN_MACOS = (26, 2)
EXO_APP_PATH = Path("/Applications/EXO.app")
EXO_DMG_URL = "https://assets.exolabs.net/EXO-latest.dmg"
_BREW_CANDIDATES = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")

RDMA_GUIDE: tuple[str, ...] = (
    "1. Shut down your Mac.",
    "2. Hold down the power button for 10 seconds until the boot menu appears.",
    '3. Select "Options" to enter Recovery mode.',
    "4. When the Recovery UI appears, open the Terminal from the Utilities menu.",
    "5. In the Terminal, type: rdma_ctl enable  and press Enter.",
    "6. Reboot your Mac.",
    "Every Mac must run the exact same macOS build, and each Mac must be cabled to every other Mac with TB5 cables.",
)


def macos_version() -> tuple[int, ...] | None:
    if platform.system() != "Darwin":
        return None
    raw = platform.mac_ver()[0]
    try:
        return tuple(int(part) for part in raw.split("."))
    except ValueError:
        return None


def require_supported_macos() -> None:
    version = macos_version()
    if version is None or version < MIN_MACOS:
        shown = ".".join(map(str, version)) if version else "unknown"
        raise ClusterError(f"exo needs macOS 26.2 or later (this Mac runs {shown})")


def exo_installed() -> bool:
    return (EXO_APP_PATH / "Contents" / "Resources" / "exo").is_dir()


def exo_version() -> str | None:
    try:
        with (EXO_APP_PATH / "Contents" / "Info.plist").open("rb") as handle:
            version = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None
    return version if isinstance(version, str) and version else None


def _brew() -> str | None:
    found = shutil.which("brew")
    if found:
        return found
    return next((path for path in _BREW_CANDIDATES if Path(path).is_file()), None)


def _brew_failure_reason(output: str) -> str:
    # brew lists every directory it cannot write; the list says nothing to a person.
    if "not writable" in output:
        return "Homebrew's folders belong to another macOS account on this Mac"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def install_exo(on_output: Callable[[str], None], *, run=subprocess.run) -> None:
    """brew's cask, else exo's own DMG (brew can be missing or owned by another account)."""
    brew = _brew()
    if brew is not None:
        # stdout stays on the terminal: the download takes minutes, and a
        # silent wait looked like a hang (node4, 2026-10-30).
        on_output("Installing exo with Homebrew (about 400 MB, this can take a few minutes)...")
        result = run([brew, "install", "--cask", "exo"], stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode == 0:
            return
        on_output(f"Homebrew could not install exo ({_brew_failure_reason(result.stderr or '')}); "
                  f"downloading it from {EXO_DMG_URL} instead...")
    else:
        on_output(f"Downloading exo from {EXO_DMG_URL} (about 400 MB)...")
    _install_exo_dmg(run)
    on_output(f"exo installed in {EXO_APP_PATH.parent}.")


def _install_exo_dmg(run) -> None:
    # Copied with ditto, so the app carries no quarantine flag (curl sets none).
    with tempfile.TemporaryDirectory(prefix="omm-exo-") as scratch:
        dmg, mount = Path(scratch) / "EXO.dmg", Path(scratch) / "mount"
        if run(["/usr/bin/curl", "-fL", "--progress-bar", "-o", str(dmg), EXO_DMG_URL], check=False).returncode != 0:
            raise ClusterError(f"could not download exo; install it by hand from {EXO_DMG_URL}")
        attach = ["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-quiet", "-mountpoint", str(mount), str(dmg)]
        if run(attach, capture_output=True, text=True, check=False).returncode != 0:
            raise ClusterError(f"could not open the exo disk image; install it by hand from {EXO_DMG_URL}")
        try:
            copy = run(["/usr/bin/ditto", str(mount / "EXO.app"), str(EXO_APP_PATH)],
                       capture_output=True, text=True, check=False)
        finally:
            run(["/usr/bin/hdiutil", "detach", "-quiet", str(mount)], capture_output=True, text=True, check=False)
        if copy.returncode != 0:
            raise ClusterError(f"could not copy EXO.app into {EXO_APP_PATH.parent}: {(copy.stderr or '').strip()}")


def runtime_dir() -> Path:
    return config.OMM_HOME / "cluster" / "exo-runtime"


def runtime_stamp_path() -> Path:
    return config.OMM_HOME / "cluster" / "exo-runtime.version"


def stage_runtime(*, run=subprocess.run) -> Path:
    """Copy the bundled exo runtime out of the app, once per exo version."""
    target = runtime_dir()
    version = exo_version() or "unknown"
    stamp = runtime_stamp_path()
    if target.is_dir():
        try:
            if stamp.read_text(encoding="utf-8").strip() == version:
                return target
        except OSError:
            pass
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = EXO_APP_PATH / "Contents" / "Resources" / "exo"
    result = run(["/usr/bin/ditto", "--noqtn", str(source), str(target)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ClusterError(f"could not copy the exo runtime: {(result.stderr or '').strip()}")
    stamp.write_text(version + "\n", encoding="utf-8")
    return target


def remove_runtime() -> None:
    """Delete the staged copy (`leave`); exo itself stays in /Applications."""
    shutil.rmtree(runtime_dir(), ignore_errors=True)
    runtime_stamp_path().unlink(missing_ok=True)


def rdma_status(*, run=subprocess.run) -> Literal["enabled", "disabled", "unavailable"]:
    binary = shutil.which("rdma_ctl")
    if binary is None:
        return "unavailable"
    try:
        result = run([binary, "status"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    word = (result.stdout or "").strip().casefold()
    return word if word in {"enabled", "disabled"} else "unavailable"
