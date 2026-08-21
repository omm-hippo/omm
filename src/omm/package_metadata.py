"""Installed distribution metadata and installation-source detection.

The import package and command remain ``omm``, but the PyPI distribution is
named ``omm-model``.  Keep that distinction in one place so callers
do not accidentally query the unrelated distribution that already owns the
``omm`` name on PyPI.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DISTRIBUTION_NAME = "omm-model"
LEGACY_DISTRIBUTION_NAMES = ("omm",)

_CONSOLE_ENTRY_POINT_GROUP = "console_scripts"
_CONSOLE_ENTRY_POINT_NAME = "omm"
_CONSOLE_ENTRY_POINT_VALUE = "omm.cli:main"
_ALLOWED_GITHUB_REPOSITORIES = {
    ("omm-hippo", "omm"),
    # Repository names used before the project moved to omm-hippo/omm.
    ("minigu5", "omm"),
    ("minigu5", "localfit"),
}
_ORIGIN_POPEN = subprocess.Popen


class InstallSource(str, Enum):
    """How the currently executing OMM code was installed."""

    GIT = "git"
    PIPX = "pipx"
    HOMEBREW = "homebrew"
    WINGET = "winget"
    NPM = "npm"
    PYPI = "pypi"
    UNKNOWN = "unknown"


_NPM_PACKAGE_ROOT_ENV = "OMM_NPM_PACKAGE_ROOT"
_NPM_LAUNCHER_PACKAGE_ENV = "OMM_NPM_LAUNCHER_PACKAGE"
_NPM_LAUNCHER_PACKAGE = "@omm-hippo/omm"
_NPM_TARGETS = {
    ("darwin", "arm64"): (
        "@omm-hippo/omm-darwin-arm64",
        "darwin-arm64",
        "bin/omm",
    ),
    ("darwin", "x64"): (
        "@omm-hippo/omm-darwin-x64",
        "darwin-x64",
        "bin/omm",
    ),
    ("linux", "arm64"): (
        "@omm-hippo/omm-linux-arm64-gnu",
        "linux-arm64-gnu",
        "bin/omm",
    ),
    ("linux", "x64"): (
        "@omm-hippo/omm-linux-x64-gnu",
        "linux-x64-gnu",
        "bin/omm",
    ),
    ("win32", "x64"): (
        "@omm-hippo/omm-win32-x64",
        "win32-x64",
        "bin/omm.exe",
    ),
}


def _legacy_distribution_is_ours(distribution: importlib.metadata.Distribution) -> bool:
    """Reject the unrelated PyPI project named ``omm``.

    Older OMM builds are accepted only when their installed console-script
    metadata proves that the ``omm`` command enters this package.
    """

    try:
        entry_points = distribution.entry_points
    except Exception:
        return False
    return any(
        entry_point.group == _CONSOLE_ENTRY_POINT_GROUP
        and entry_point.name == _CONSOLE_ENTRY_POINT_NAME
        and entry_point.value == _CONSOLE_ENTRY_POINT_VALUE
        for entry_point in entry_points
    )


def find_distribution() -> tuple[str, importlib.metadata.Distribution] | None:
    """Return OMM's installed distribution and the name used to find it.

    The new, unambiguous distribution name always wins.  The legacy name is
    only a compatibility fallback and must pass the console-entry-point check
    above before it can be treated as this project.
    """

    try:
        return DISTRIBUTION_NAME, importlib.metadata.distribution(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass

    for name in LEGACY_DISTRIBUTION_NAMES:
        try:
            candidate = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        if _legacy_distribution_is_ours(candidate):
            return name, candidate
    return None


def distribution() -> importlib.metadata.Distribution:
    """Return OMM's distribution or raise ``PackageNotFoundError``."""

    found = find_distribution()
    if found is None:
        raise importlib.metadata.PackageNotFoundError(DISTRIBUTION_NAME)
    return found[1]


def version() -> str:
    """Return the installed OMM version under the current or legacy name."""

    return distribution().version


def direct_url(
    installed_distribution: importlib.metadata.Distribution | None = None,
) -> dict[str, Any] | None:
    """Read pip's PEP 610 ``direct_url.json`` for this OMM installation."""

    try:
        dist = installed_distribution or distribution()
        raw = dist.read_text("direct_url.json")
    except (importlib.metadata.PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _package_checkout() -> Path:
    # src/omm/package_metadata.py -> repository root for a source checkout.
    return Path(__file__).resolve().parents[2]


def _installation_paths(
    installed_distribution: importlib.metadata.Distribution | None,
) -> list[Path]:
    paths = [Path(__file__).absolute(), Path(sys.prefix).absolute(), Path(sys.executable).absolute()]
    if installed_distribution is not None:
        try:
            paths.append(Path(installed_distribution.locate_file("")).absolute())
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return paths


def _normalized_path(path: Path) -> str:
    return str(path).replace("\\", "/").casefold().rstrip("/")


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.absolute().relative_to(parent.absolute())
    except (OSError, ValueError):
        return False
    return True


def _npm_machine() -> str | None:
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return None


def _npm_target() -> tuple[str, str, str, str, str] | None:
    machine = _npm_machine()
    if machine is None:
        return None
    target = _NPM_TARGETS.get((sys.platform, machine))
    if target is None:
        return None
    package_name, target_name, binary = target
    return package_name, target_name, binary, sys.platform, machine


def _npm_install_is_verified(
    installed_distribution: importlib.metadata.Distribution | None,
) -> bool:
    """Require the launcher, package manifest, version, and binary to agree.

    The JavaScript launcher sets the package root before starting the bundled
    executable.  Treating that environment variable alone as proof would let
    an unrelated wrapper claim npm ownership, so the adjacent npm manifest and
    the currently executing binary must also match the declared target.
    """

    raw_root = os.environ.get(_NPM_PACKAGE_ROOT_ENV)
    launcher = os.environ.get(_NPM_LAUNCHER_PACKAGE_ENV)
    target = _npm_target()
    if (
        not raw_root
        or launcher != _NPM_LAUNCHER_PACKAGE
        or installed_distribution is None
        or target is None
    ):
        return False

    package_name, target_name, binary_name, expected_os, expected_cpu = target
    root_path = Path(raw_root)
    if not root_path.is_absolute():
        return False
    try:
        root = root_path.resolve(strict=True)
        manifest_path = root / "package.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False

    metadata = manifest.get("omm")
    if not isinstance(metadata, dict):
        return False
    if (
        manifest.get("name") != package_name
        or manifest.get("version") != installed_distribution.version
        or manifest.get("os") != [expected_os]
        or manifest.get("cpu") != [expected_cpu]
        or metadata.get("distribution") != DISTRIBUTION_NAME
        or metadata.get("target") != target_name
        or metadata.get("binary") != binary_name
    ):
        return False
    if expected_os == "linux" and manifest.get("libc") != ["glibc"]:
        return False

    binary_path = root / binary_name
    if not binary_path.is_file() or binary_path.is_symlink():
        return False
    try:
        binary = binary_path.resolve(strict=True)
        executable = Path(sys.executable).resolve(strict=True)
    except OSError:
        return False
    if not _is_inside(binary, root):
        return False
    return executable == binary


def _allowed_git_origin(url: object) -> bool:
    """Accept only the canonical OMM repository and its historical names.

    Git supports both URL syntax and the SCP-like ``git@host:owner/repo``
    syntax.  Parse those forms explicitly so a lookalike host, nested path,
    query, fragment, user name, or port cannot be mistaken for this project.
    """

    if not isinstance(url, str) or not url or url != url.strip():
        return False

    scp_match = re.fullmatch(r"git@github\.com:([^/?#]+)/([^/?#]+)", url)
    if scp_match:
        owner, repository = scp_match.groups()
    else:
        try:
            parsed = urlsplit(url)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError:
            return False
        if parsed.query or parsed.fragment or port is not None:
            return False
        if parsed.scheme == "https":
            if parsed.username is not None or parsed.password is not None:
                return False
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None:
                return False
        else:
            return False
        if hostname != "github.com":
            return False
        path_match = re.fullmatch(r"/([^/]+)/([^/]+)", parsed.path)
        if path_match is None:
            return False
        owner, repository = path_match.groups()

    if repository.endswith(".git"):
        repository = repository[:-4]
    return (owner.casefold(), repository.casefold()) in _ALLOWED_GITHUB_REPOSITORIES


def _checkout_origin(checkout: Path) -> str | None:
    """Read one checkout origin without allowing Git to block the CLI."""

    try:
        process = _ORIGIN_POPEN(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    try:
        stdout, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    if process.returncode != 0:
        return None
    origin = stdout.strip()
    return origin or None


def install_source() -> InstallSource:
    """Best-effort classification of the currently executing OMM install.

    Git is returned only with positive evidence that the executing module
    belongs to the canonical OMM repository (or one of its historical names):
    a checkout with an exact allowed ``origin``, or matching Git VCS data in
    PEP 610 metadata. Every other result is package-managed (or unknown), so
    the CLI can refuse to replace it with an editable Git checkout.
    """

    checkout = _package_checkout()
    if (checkout / ".git").exists():
        origin = _checkout_origin(checkout)
        return InstallSource.GIT if _allowed_git_origin(origin) else InstallSource.UNKNOWN

    found = find_distribution()
    installed_distribution = found[1] if found is not None else None
    install_record = direct_url(installed_distribution) if installed_distribution else None
    vcs_info = install_record.get("vcs_info") if install_record else None
    if isinstance(vcs_info, dict) and vcs_info.get("vcs") == "git":
        return (
            InstallSource.GIT
            if _allowed_git_origin(install_record.get("url"))
            else InstallSource.UNKNOWN
        )

    paths = _installation_paths(installed_distribution)
    normalized_paths = [_normalized_path(path) for path in paths]

    npm_claimed = bool(
        os.environ.get(_NPM_PACKAGE_ROOT_ENV)
        or os.environ.get(_NPM_LAUNCHER_PACKAGE_ENV)
    )
    if npm_claimed:
        return (
            InstallSource.NPM
            if _npm_install_is_verified(installed_distribution)
            else InstallSource.UNKNOWN
        )

    pipx_home = os.environ.get("PIPX_HOME")
    if (Path(sys.prefix) / "pipx_metadata.json").is_file() or any(
        "/pipx/venvs/" in f"{path}/" for path in normalized_paths
    ):
        return InstallSource.PIPX
    if pipx_home and any(_is_inside(path, Path(pipx_home) / "venvs") for path in paths):
        return InstallSource.PIPX

    if any(
        marker in f"{path}/"
        for path in normalized_paths
        for marker in (
            "/cellar/omm-model/",
            "/cellar/omm/",
            "/opt/omm-model/",
            "/opt/omm/",
        )
    ):
        return InstallSource.HOMEBREW

    if any(
        "/winget/packages/ommhippo.omm_" in f"{path}/" for path in normalized_paths
    ):
        return InstallSource.WINGET

    if installed_distribution is not None:
        return InstallSource.PYPI
    return InstallSource.UNKNOWN
