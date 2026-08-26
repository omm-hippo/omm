"""Zero-duplication linker: symlink central .gguf files into every local AI
app omm knows about, without copying bytes.

Ollama's on-disk manifest format is not officially documented. The shape
used here (schemaVersion 2, OCI-style config+layers) was captured by
running `ollama create` on a bare GGUF file and inspecting the resulting
manifest/config/blobs directly, and may need updates if Ollama changes it.

AnythingLLM's built-in local model provider is a private, bundled Ollama
instance (its own copy of the ollama binary with OLLAMA_MODELS pointed at
AnythingLLM's own storage dir) - confirmed by inspecting a real AnythingLLM
0.10 install's process list and env, and by linking a test model into that
directory and seeing it appear in the embedded server's /api/tags. So it
reuses the same manifest/blob functions as system Ollama, just pointed at a
different models_dir.

Jan (llamacpp-extension) requires a model.yml manifest per model folder
under <jan data>/llamacpp/models/<model_id>/ - a bare dropped .gguf is not
recognized. Its own local-file import writes model_path as an absolute path
with no copy/symlink of its own, so omm does the same instead of also
placing a symlink inside Jan's tree. Confirmed by reading the extension's
actual shipped source (menloresearch/jan, llamacpp-extension/src/index.ts).

Msty (MstyStudio) and text-generation-webui both recognize a flat directory
of .gguf files (any depth for Msty/text-generation-webui), including
symlinks - Msty's own "import local GGUF" feature symlinks into
`<userData>/models` itself (confirmed by extracting and reading Msty's
shipped app.asar), and text-generation-webui's model list walks its models
dir with follow_links=True (confirmed by reading its actual source). Both
reuse link_custom_directory/unlink_custom_directory.

KoboldCpp has no fixed install location (portable binary) and by default
has no directory it scans for models at all - only `--admindir` (which the
user must opt into explicitly) does a real directory scan. For safety, omm
only discovers a manually approved copy placed under `<OMM_HOME>/apps`,
then links into a `models` folder next to it; common writable directories
are never searched automatically.

text-generation-webui likewise has no fixed OS install location. omm only
checks `<OMM_HOME>/apps` for either of two known layouts: an old-style git
clone (server.py + one_click.py at the root) or a portable prebuilt release
(server.py under app/, no one_click.py at all).
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import platform
import re
import shlex
import shutil
import stat as stat_module
import tarfile
import time
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.gguf import read_gguf_metadata
from omm.hardware import HardwareInfo
from omm.hashutil import sha256_file
from omm.atomic import atomic_write_bytes, atomic_write_text, backup_corrupt_file, locked
from omm.config import LINK_OWNERSHIP_PATH, MODELS_DIR


def lmstudio_home_dir() -> Path:
    """LM Studio's data dir. Newer versions default to ~/.lmstudio, but keep
    using ~/.cache/lm-studio (the old default) if that's what's already
    there - LM Studio itself does this via a ~/.lmstudio-home-pointer file
    when it finds a pre-existing legacy install. Confirmed on a real 0.4.19
    Homebrew install where the pointer redirected to ~/.cache/lm-studio.
    """
    pointer = Path.home() / ".lmstudio-home-pointer"
    if pointer.exists():
        try:
            value = pointer.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            value = ""
        if value:
            target = Path(value).expanduser()
            return target if target.is_absolute() else Path.home() / target
    if (Path.home() / ".cache" / "lm-studio").exists():
        return Path.home() / ".cache" / "lm-studio"
    return Path.home() / ".lmstudio"


def lmstudio_models_dir() -> Path:
    override = os.environ.get("OMM_LMSTUDIO_MODELS_DIR")
    if override:
        return Path(override).expanduser()
    return lmstudio_home_dir() / "models"


def _systemd_ollama_models_dir() -> Path | None:
    """The official Linux installer runs `ollama serve` under systemd as a
    dedicated `ollama` system user, whose $HOME differs from the invoking
    CLI user's - so Path.home() below resolves to a directory the daemon
    never reads from (issue #117). When ollama.service is actually active,
    ask systemd for its real User= and any unit-level OLLAMA_MODELS
    override instead of guessing; return None to fall through to today's
    behavior everywhere else (macOS, Windows, manual `ollama serve`,
    Docker, or the service simply not running)."""
    if platform.system() != "Linux" or shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "show", "ollama.service", "--property=ActiveState,User,Environment"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    if props.get("ActiveState") != "active":
        return None
    # systemd prints Environment= as a shell-quoted, space-separated list, so
    # an entry whose value contains spaces comes back quoted as a single word
    # ("OLLAMA_MODELS=/mnt/ollama models"). A plain str.split() would slice
    # that path in half at the first space and hand back a directory that
    # doesn't exist; shlex.split() unquotes it the way a shell would. Fall
    # back to a naive split if the quoting is somehow unbalanced, so a weird
    # unit can't take ollama_models_dir() down with a ValueError.
    environment = props.get("Environment", "")
    try:
        env_pairs = shlex.split(environment)
    except ValueError:
        env_pairs = environment.split()
    for env_pair in env_pairs:
        key, _, value = env_pair.partition("=")
        if key == "OLLAMA_MODELS" and value:
            return Path(value).expanduser()
    import pwd  # POSIX-only; safe here since we already required Linux above

    try:
        home = pwd.getpwnam(props.get("User") or "root").pw_dir
    except KeyError:
        return None
    return Path(home) / ".ollama" / "models"


def ollama_models_dir() -> Path:
    systemd_dir = _systemd_ollama_models_dir()
    if systemd_dir is not None:
        return systemd_dir
    env_dir = os.environ.get("OLLAMA_MODELS")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".ollama" / "models"


_APP_BUNDLE_SEARCH_ROOTS = [Path("/Applications"), Path.home() / "Applications"]


def _app_bundle_installed(app_name: str) -> bool:
    """macOS-only: whether <app_name>.app is actually present in
    /Applications or ~/Applications. Deleting an app (drag to Trash)
    removes the bundle but leaves its data dir under ~/Library/Application
    Support (or, for LM Studio, ~/.lmstudio / ~/.cache/lm-studio) behind -
    so a bundle check catches an uninstall that a data-dir check misses.
    No equivalent bundle convention exists on Windows/Linux, so those
    platforms fall back to the data-dir check instead."""
    if platform.system() != "Darwin":
        return False
    return any((root / f"{app_name}.app").exists() for root in _APP_BUNDLE_SEARCH_ROOTS)


_DESKTOP_ENTRY_SEARCH_ROOTS = [
    Path.home() / ".local" / "share" / "applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]


def _windows_install_artifact_exists(dir_names: Sequence[str], shortcut_glob: str) -> bool:
    """Windows-only: whether an installer left install-*time* artifacts behind.

    An Electron app's userData directory (%APPDATA%\\<product>) is created the
    first time the app *launches*, not when it is installed - so checking it
    alone reports an installed-but-never-launched app as "not installed".
    The installer, by contrast, writes its program directory and Start Menu
    shortcut during install. Probing those is the Windows counterpart of
    _app_bundle_installed on Darwin and of the `flatpak info` check
    is_jan_installed uses on Linux.

    `dir_names` are checked under the electron-builder per-user default
    (%LOCALAPPDATA%\\Programs) and under %ProgramFiles% for a machine-wide
    install; `shortcut_glob` is matched in both the per-user and the
    all-users Start Menu, at the top level and one folder deep (installers
    put shortcuts either directly in Programs or in their own subfolder).
    """
    if platform.system() != "Windows":
        return False
    program_roots = [engine_install_dir()]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        program_roots.append(Path(local_app_data) / "Programs")
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value:
            program_roots.append(Path(value))
    for root in program_roots:
        for name in dir_names:
            # A failed or manually cancelled installer can leave an empty
            # folder behind; only a folder holding an .exe counts as installed.
            try:
                if any(p.suffix.lower() == ".exe" for p in (root / name).iterdir()):
                    return True
            except OSError:
                continue
    for variable in ("APPDATA", "ProgramData"):
        value = os.environ.get(variable)
        if not value:
            continue
        menu = Path(value) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for pattern in (shortcut_glob, f"*/{shortcut_glob}"):
            try:
                if any(menu.glob(pattern)):
                    return True
            except OSError:
                continue
    return False


def _linux_install_artifact_exists(install_dirs: Sequence[Path], desktop_entry_glob: str) -> bool:
    """Linux-only: same install-vs-first-run distinction as
    _windows_install_artifact_exists. ~/.config/<product> only appears once
    the app has been run, while the installer writes its own program
    directory and a freedesktop .desktop entry up front."""
    if platform.system() != "Linux":
        return False
    for directory in install_dirs:
        try:
            if directory.is_dir():
                return True
        except OSError:
            continue
    for root in _DESKTOP_ENTRY_SEARCH_ROOTS:
        try:
            if any(root.glob(desktop_entry_glob)):
                return True
        except OSError:
            continue
    return False


def is_lmstudio_installed() -> bool:
    # A headless llmster install (the `lms` CLI + daemon, no GUI) is a
    # real, usable install with no app bundle at all - check it first.
    if _lms_cli_path() is not None:
        return True
    if platform.system() == "Darwin":
        return _app_bundle_installed("LM Studio")
    return lmstudio_home_dir().exists()


def find_ollama_executable() -> Path | None:
    """Find Ollama even when a freshly installed Windows PATH is stale.

    winget writes the new PATH entry to the registry, but an already-running
    process (like the `omm setup` wizard that just triggered the install)
    keeps the PATH it started with - `shutil.which` alone stays blind to the
    fresh install until the terminal restarts. Falling back to Ollama's
    documented install locations catches it immediately instead.
    """
    on_path = shutil.which("ollama")
    if on_path:
        return Path(on_path)
    if platform.system() != "Windows":
        return None

    roots = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if local_app_data:
        roots.extend(
            [
                Path(local_app_data) / "Programs" / "Ollama",
                Path(local_app_data) / "Ollama",
            ]
        )
    if program_files:
        roots.append(Path(program_files) / "Ollama")
    for root in roots:
        candidate = root / "ollama.exe"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def is_ollama_installed() -> bool:
    # Homebrew's ollama-app cask installs Ollama.app; the plain `ollama`
    # formula (common on Linux/Homebrew-CLI setups) installs only the
    # `ollama` binary with no bundle - check both so a CLI-only install
    # isn't reported as "not installed".
    if platform.system() == "Darwin":
        return _app_bundle_installed("Ollama") or shutil.which("ollama") is not None
    return (Path.home() / ".ollama").exists() or find_ollama_executable() is not None


class LinkError(Exception):
    """`fix`, when set, is a copy-pasteable next step for the CLI's
    cause+fix error format (issue #191) - callers that don't have one
    just omit it and get the old single-message behavior."""

    def __init__(self, message: str, *, fix: str | None = None):
        self.fix = fix
        super().__init__(message)


def _link_key(path: Path) -> str:
    """Stable key without resolving a link target that may no longer exist."""
    return str(path.expanduser().absolute())


def _engine_path_lock(path: Path) -> Path:
    """Lock proxy for a path inside an engine-owned directory (e.g. Ollama's
    blobs/manifests dirs). `locked()` places a `.lock` sibling right next to
    the path it protects, which is fine inside omm's own OMM_HOME but would
    otherwise litter a third-party engine's directory with a stray `.lock`
    file forever - so lock a same-named proxy under OMM_HOME/locks instead.
    Pass the return value straight to `locked()`, which appends `.lock`."""
    digest = hashlib.sha256(_link_key(path).encode()).hexdigest()
    return config.OMM_HOME / "locks" / digest


def _models_transaction_lock(models_dir: Path) -> Path:
    """One lock covering manifest publication and orphan-blob reclamation.

    Per-blob and per-manifest locks prevent torn individual writes, but are
    not enough for the multi-file invariant: an unlink scanning references
    must not delete a just-created shared blob before another link publishes
    the manifest that references it.
    """
    return _engine_path_lock(models_dir / ".omm-link-transaction")


def _load_link_ownership() -> dict[str, dict[str, object]]:
    if not LINK_OWNERSHIP_PATH.exists():
        return {}
    try:
        data = json.loads(LINK_OWNERSHIP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup_corrupt_file(LINK_OWNERSHIP_PATH)
        return {}
    if not isinstance(data, dict):
        backup_corrupt_file(LINK_OWNERSHIP_PATH)
        return {}
    return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, dict)}


def _update_link_ownership(path: Path, ownership: dict[str, object] | None) -> None:
    """Atomically add or remove a hard-link ownership record."""
    key = _link_key(path)
    with locked(LINK_OWNERSHIP_PATH):
        records = _load_link_ownership()
        if ownership is None:
            records.pop(key, None)
        else:
            records[key] = ownership
        atomic_write_text(LINK_OWNERSHIP_PATH, json.dumps(records, indent=2) + "\n")


def _bulk_clear_link_ownership(paths: Sequence[Path]) -> None:
    """Remove several ownership records in one locked read-modify-write.

    An autoremove pass can find many broken links at once; clearing each
    one through `_update_link_ownership` individually would reload and
    rewrite the *entire* on-disk registry once per removed path (an O(n)
    full-file read+write for n unrelated removals found in the same pass).
    This reaches the same end state - every key in `paths` is gone from
    the registry - with a single lock acquisition, read, and write no
    matter how many paths are being cleared.
    """
    if not paths:
        return
    keys = [_link_key(path) for path in paths]
    with locked(LINK_OWNERSHIP_PATH):
        records = _load_link_ownership()
        for key in keys:
            records.pop(key, None)
        atomic_write_text(LINK_OWNERSHIP_PATH, json.dumps(records, indent=2) + "\n")


def _record_ownership(dst: Path, src: Path | None, kind: str) -> None:
    record = {
        "kind": kind,
        "source": _link_key(src) if src is not None else None,
    }
    if kind == "symlink":
        # Use lstat: a broken link has no target to stat, but its own file
        # identity still lets us prove it is the link omm created.
        stat = dst.lstat()
    else:
        stat = dst.stat()
        # An attacker or another program can replace a regular file at
        # this path.  Device/inode make the ownership claim apply only to
        # the exact hard link we made, never a later ordinary file.
    record.update({"device": stat.st_dev, "inode": stat.st_ino})
    if kind == "copy":
        record.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    if kind == "manifest":
        record["content_sha256"] = sha256_file(dst)
    _update_link_ownership(
        dst,
        record,
    )


def _record_hardlink(dst: Path, src: Path) -> None:
    _record_ownership(dst, src, "hardlink")


def _record_symlink(dst: Path, src: Path) -> None:
    _record_ownership(dst, src, "symlink")


def _ownership_record(path: Path) -> dict[str, object] | None:
    """Look up `path`'s ownership record, tolerating a casing mismatch on
    Windows.

    `_link_key` never normalizes case, so a custom-directory path typed
    with different capitalization across two `omm` invocations (NTFS
    itself is case-insensitive; the exact string typed is not) would
    otherwise miss its own registry entry entirely - `link_file` would
    then treat an omm-owned link as unrecorded and could refuse to touch
    it ("Refusing to replace unowned existing file"). Every caller of this
    helper still re-verifies device/inode (or content hash) before trusting
    the record, so a case-insensitive match can never be mistaken for a
    different file's ownership - it only recovers a record that's
    provably for this exact path.
    """
    records = _load_link_ownership()
    key = _link_key(path)
    record = records.get(key)
    if record is not None or platform.system() != "Windows":
        return record
    folded = key.casefold()
    for other_key, other_record in records.items():
        if other_key.casefold() == folded:
            return other_record
    return None


def _owned_hardlink(path: Path, record: dict[str, object] | None = None) -> bool:
    if record is None:
        record = _ownership_record(path)
    if not record or record.get("kind") != "hardlink" or path.is_symlink() or not path.exists():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return record.get("device") == stat.st_dev and record.get("inode") == stat.st_ino


def _owned_symlink(path: Path, record: dict[str, object] | None = None) -> bool:
    if record is None:
        record = _ownership_record(path)
    if not record or record.get("kind") != "symlink" or not path.is_symlink():
        return False
    if "device" in record and "inode" in record:
        try:
            stat = path.lstat()
        except OSError:
            return False
        return record.get("device") == stat.st_dev and record.get("inode") == stat.st_ino
    # Records written before symlink identities were retained remain
    # compatible, but their target text is the only available proof.
    try:
        target = Path(os.readlink(path))
        if not target.is_absolute():
            target = path.parent / target
        return _link_key(target) == record.get("source")
    except OSError:
        return False


def _owned_copy(path: Path, record: dict[str, object] | None = None) -> bool:
    if record is None:
        record = _ownership_record(path)
    if not record or record.get("kind") != "copy" or path.is_symlink() or not path.exists():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return (
        record.get("device") == stat.st_dev
        and record.get("inode") == stat.st_ino
        and record.get("size") == stat.st_size
        and record.get("mtime_ns") == stat.st_mtime_ns
    )


def _matches_requested_link(src: Path, dst: Path) -> bool:
    """Whether an old unrecorded destination is provably the requested link."""
    if dst.is_symlink():
        try:
            target = Path(os.readlink(dst))
            if not target.is_absolute():
                target = dst.parent / target
            return _link_key(target) == _link_key(src)
        except OSError:
            return False
    try:
        return dst.samefile(src)
    except OSError:
        return False


def _owned_manifest(
    path: Path, expected_source: Path | None = None, record: dict[str, object] | None = None
) -> bool:
    if record is None:
        record = _ownership_record(path)
    if not record or record.get("kind") != "manifest" or not path.exists() or path.is_symlink():
        return False
    # A manifest written by _fallback_to_native_create has source=None -
    # `ollama create` remaps the model layer to its own content digest, so
    # there is no gguf path to record. That manifest is still omm's own
    # (the model_name-derived path already disambiguates which model it
    # belongs to), so only reject a source mismatch when the record
    # actually names a different source.
    record_source = record.get("source")
    if expected_source is not None and record_source is not None and record_source != _link_key(expected_source):
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if record.get("device") != stat.st_dev or record.get("inode") != stat.st_ino:
        return False
    # Records written before content_sha256 tracking was added (pre-2026-07-31)
    # have no such key at all - fall back to the device/inode check alone
    # rather than treating an absent key as a guaranteed mismatch.
    if "content_sha256" not in record:
        return True
    try:
        content_sha256 = sha256_file(path)
    except OSError:
        return False
    return record.get("content_sha256") == content_sha256


def unlink_owned_link(
    path: Path, expected_source: Path | None = None, record: dict[str, object] | None = None
) -> bool:
    """Remove an omm symlink or a recorded, unchanged omm hard link.

    Never removes an unrecorded regular file.  Returns whether a link was
    removed so callers can preserve ordinary user files at managed paths.

    `record` lets a caller that already loaded this path's ownership
    record (e.g. `link_file`) pass it straight through instead of making
    this function - and each of the `_owned_*` checks below - reload and
    re-parse the same on-disk registry file for the same path.
    """
    if record is None:
        record = _ownership_record(path)
    if expected_source is not None:
        if not record or record.get("source") != _link_key(expected_source):
            return False
    if _owned_symlink(path, record):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    if _owned_hardlink(path, record):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    if _owned_copy(path, record):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    return False


def _unlink_owned_link_with_retry(
    path: Path, expected_source: Path | None = None, attempts: int = 8
) -> bool:
    """Retry an unlink against a Windows sharing violation (WinError 32):
    a model file just unloaded by Ollama/LM Studio/a custom app can hold
    its handle open for a moment after the engine reports it as stopped."""
    for attempt in range(attempts):
        try:
            return unlink_owned_link(path, expected_source=expected_source)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.1 * (2**attempt), 1.0))
    return False


def autoremove_owned_link(path: Path) -> bool:
    """Public custom-directory equivalent of the engine autoremove helpers."""
    if path.is_symlink() and not path.exists():
        return unlink_owned_link(path)
    # Do not automatically delete hard links.  Even a recorded hard link is
    # indistinguishable from a user-owned one after a crash or file-system
    # restore; preserving an orphan is safer than risking user data.
    return False


CopyReporter = Callable[[Path, Path, int], None]


class InsufficientLinkSpaceError(LinkError):
    """An engine link would exhaust its destination volume."""


def disk_safety_reserve(size_bytes: int) -> int:
    """Keep enough headroom for metadata, logs, and concurrent small writes."""
    return min(max(1024**3, size_bytes // 20), 4 * 1024**3)


def _is_disk_full_error(error: OSError) -> bool:
    return error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}


def _existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def storage_volume_key(path: Path) -> tuple[str, str | int]:
    """Stable volume identity for grouping preflight disk requirements."""
    resolved = path.expanduser().resolve(strict=False)
    if platform.system() == "Windows":
        return ("windows", resolved.drive.casefold())
    existing = _existing_parent(resolved)
    try:
        return ("device", existing.stat().st_dev)
    except OSError:
        return ("anchor", resolved.anchor)


def disk_usage_path(path: Path) -> Path:
    """Nearest existing path accepted by ``shutil.disk_usage``."""
    return _existing_parent(path)


def link_file(
    src: Path, dst: Path, *, on_copy: CopyReporter | None = None, force: bool = False
) -> str:
    """Expose ``src`` at ``dst`` and return symlink/hardlink/copy.

    Windows file junctions are not applicable here (they only target
    directories). A hard link is attempted first because it needs no
    Developer Mode; a symlink covers cross-volume destinations when
    permitted, and an ownership-recorded copy is the last-resort fallback.

    `force` reclaims a destination omm does not recognize as its own
    (e.g. the ownership registry was lost, or the file was linked by
    something else) by deleting it outright instead of raising. Callers
    pass this through only when the user explicitly opted in.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LinkError(f"Could not create directory {dst.parent}: {e}") from e
    if dst.exists() or dst.is_symlink():
        # Already exactly this link (same recorded source, same untouched
        # symlink/hardlink/copy identity) - skip the delete+recreate and its
        # ownership-registry rewrite. Cheap ownership-record checks only;
        # no full-file read. Otherwise every unchanged model got its
        # link torn down and rebuilt on every repeat `omm link`/`install` -
        # for a "copy" fallback destination (Windows without Developer Mode,
        # or a cross-volume custom directory) that meant a full multi-GB
        # shutil.copy2 every time, even though nothing had changed.
        record = _ownership_record(dst)
        if record and record.get("source") == _link_key(src):
            if record.get("kind") == "symlink" and _owned_symlink(dst, record):
                return "symlink"
            if record.get("kind") == "hardlink" and _owned_hardlink(dst, record):
                return "hardlink"
            if record.get("kind") == "copy" and _owned_copy(dst, record):
                return "copy"
        if not unlink_owned_link(dst, expected_source=src, record=record):
            if record and record.get("kind") in {"symlink", "hardlink"}:
                raise LinkError(
                    f"Refusing to replace an omm link for a different model at {dst}."
                )
            # Pre-ownership-registry omm links have no metadata. During an
            # explicit relink, adopt only a destination proved to be this
            # exact source; never infer ownership from a matching filename.
            if _matches_requested_link(src, dst):
                if dst.is_symlink():
                    _record_symlink(dst, src)
                else:
                    _record_hardlink(dst, src)
                return "symlink" if dst.is_symlink() else "hardlink"
            if not force:
                raise LinkError(f"Refusing to replace unowned existing file at {dst}.")
            dst.unlink()

    errors = []
    if platform.system() == "Windows":
        try:
            dst.hardlink_to(src)
            try:
                _record_hardlink(dst, src)
            except Exception:
                dst.unlink(missing_ok=True)
                raise
            return "hardlink"
        except OSError as error:
            errors.append(f"hard link: {error}")

    try:
        dst.symlink_to(src)
        try:
            _record_symlink(dst, src)
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        return "symlink"
    except OSError as error:
        errors.append(f"symbolic link: {error}")
        if platform.system() != "Windows":
            raise LinkError(f"Could not create symlink at {dst}: {error}.") from error

    try:
        try:
            source_size = src.stat().st_size
            free_bytes = shutil.disk_usage(dst.parent).free
        except OSError as error:
            raise LinkError(
                f"Could not verify free space before copying {src} to {dst}: {error}."
            ) from error
        reserve = min(max(64 * 1024**2, source_size // 20), 1024**3)
        required = source_size + reserve
        if free_bytes < required:
            raise InsufficientLinkSpaceError(
                f"A real copy is required at {dst}, but the destination has only "
                f"{free_bytes / 1024**3:.1f} GiB free; the {source_size / 1024**3:.1f} GiB "
                f"model needs at least {required / 1024**3:.1f} GiB including safety space."
            )
        shutil.copy2(src, dst)
        try:
            _record_ownership(dst, src, "copy")
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        if on_copy is not None:
            on_copy(src, dst, source_size)
        return "copy"
    except OSError as error:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        errors.append(f"copy: {error}")
        if _is_disk_full_error(error):
            raise InsufficientLinkSpaceError(
                f"The destination volume filled up while copying the model to {dst}. "
                "The incomplete copy was removed."
            ) from error
        raise LinkError(
            f"Could not expose the model at {dst} ({'; '.join(errors)})."
        ) from error


# --- LM Studio -------------------------------------------------------------
#
# LM Studio only recognizes models laid out as models/<publisher>/<repo>/
# <file>.gguf (mirrors the HuggingFace repo layout) - a flat models/<file>.gguf
# is silently ignored by its scanner. Confirmed against a real LM Studio 0.4.19
# install via its bundled `lms ls` CLI.


def _lmstudio_publisher_repo(repo_id: str | None, filename: str) -> tuple[str, str]:
    if repo_id:
        parts = repo_id.split("/")
        if (
            len(parts) != 2
            or any(
                not part
                or part in {".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
                for part in parts
            )
        ):
            raise LinkError("Unsafe model repository id.")
        publisher, repo = parts
        return publisher, repo
    return "local", Path(filename).stem


def link_lmstudio(
    gguf_path: Path,
    repo_id: str | None,
    *,
    on_copy: CopyReporter | None = None,
    force: bool = False,
) -> Path:
    publisher, repo = _lmstudio_publisher_repo(repo_id, gguf_path.name)
    root = lmstudio_models_dir()
    root.mkdir(parents=True, exist_ok=True)
    publisher_dir = root / publisher
    if publisher_dir.is_symlink():
        raise LinkError(f"Refusing LM Studio symlinked publisher directory: {publisher_dir}.")
    publisher_dir.mkdir(exist_ok=True)
    repo_dir = publisher_dir / repo
    if repo_dir.is_symlink():
        raise LinkError(f"Refusing LM Studio symlinked repository directory: {repo_dir}.")
    repo_dir.mkdir(exist_ok=True)
    dst = repo_dir / gguf_path.name
    link_file(gguf_path, dst, on_copy=on_copy, force=force)
    return dst


def link_custom_directory(
    gguf_path: Path,
    directory: Path,
    *,
    on_copy: CopyReporter | None = None,
    force: bool = False,
) -> Path:
    """Expose a central GGUF in an arbitrary local application's model directory."""
    destination = directory.expanduser() / gguf_path.name
    link_file(gguf_path, destination, on_copy=on_copy, force=force)
    return destination


def unlink_custom_directory(filename: str, directory: Path) -> None:
    dst = directory.expanduser() / Path(filename.replace("\\", "/")).name
    _unlink_owned_link_with_retry(dst, expected_source=MODELS_DIR / filename)


def autoremove_custom_directory(directory: Path) -> int:
    """Delete broken symlinks omm placed directly in `directory` (flat, not
    recursive - link_custom_directory never nests, so nothing else is
    omm's to clean up here). Returns the number removed."""
    directory = directory.expanduser()
    if not directory.exists():
        return 0
    removed = 0
    # Loaded once and reused for every candidate below instead of having
    # unlink_owned_link's own ownership check reload and re-parse the whole
    # on-disk registry per broken symlink found in this directory.
    ownership = _load_link_ownership()
    for path in directory.iterdir():
        if path.is_symlink() and not path.exists():
            if unlink_owned_link(path, record=ownership.get(_link_key(path))):
                removed += 1
    return removed


def unlink_lmstudio(filename: str, repo_id: str | None) -> None:
    publisher, repo = _lmstudio_publisher_repo(repo_id, filename)
    root = lmstudio_models_dir()
    dst = root / publisher / repo / Path(filename.replace("\\", "/")).name
    if not dst.parent.resolve().is_relative_to(root.resolve()):
        raise LinkError("Refusing LM Studio path outside the managed model directory.")
    if _unlink_owned_link_with_retry(dst, expected_source=MODELS_DIR / filename):
        for parent in (dst.parent, dst.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break


# --- LM Studio load verification ----------------------------------------
#
# LM Studio has no benchmark path (unlike Ollama, where `omm benchmark`
# already exercises real loading via /api/generate) - so nothing else ever
# proves a linked model actually loads. These functions send a real short
# generation request through LM Studio's own local server to check.
# Everything here fails soft: only a confirmed bad generation returns
# False; every other obstacle (lms missing, server unreachable, ambiguous
# timeout) returns None, matching _ollama_accepts_manifest's convention.


def _lms_cli_path() -> str | None:
    """Locate the `lms` CLI LM Studio bootstraps on first run. Not
    guaranteed to be on PATH in a non-interactive shell even when
    installed (same stale-PATH-right-after-install gap `find_ollama_executable`
    works around), so also check the well-known bootstrap location directly -
    confirmed via `lms bootstrap` against a real LM Studio 0.4.20 install,
    which installs to <lmstudio_home_dir>/bin/lms (lms.exe on Windows - the
    extensionless name never exists there, so it must be checked first or
    every Windows caller silently sees "not installed")."""
    found = shutil.which("lms")
    if found is not None:
        return found
    bin_dir = lmstudio_home_dir() / "bin"
    names = ["lms.exe", "lms"] if platform.system() == "Windows" else ["lms"]
    for name in names:
        candidate = bin_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def _lmstudio_server_status(lms_path: str, timeout: float = 5) -> dict | None:
    """Ask `lms` whether its local server is running and on what port -
    the port is user-configurable, so this is the only reliable source for
    it. None on any failure to ask (never guess a default port)."""
    try:
        result = subprocess.run(
            [lms_path, "server", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("running"), bool)
        or isinstance(data.get("port"), bool)
        or not isinstance(data.get("port"), int)
        or not 1 <= data["port"] <= 65_535
    ):
        return None
    return data


_LMSTUDIO_SERVER_START_TIMEOUT_SECONDS = 30
_LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS = 1


def _start_lmstudio_server(
    lms_path: str, timeout: float = _LMSTUDIO_SERVER_START_TIMEOUT_SECONDS
) -> bool:
    """Best-effort `lms server start`, polling status until it reports
    running or `timeout` elapses. Bounded - never waits indefinitely."""
    try:
        subprocess.run(
            [lms_path, "server", "start"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    elapsed = 0.0
    while elapsed < timeout:
        status = _lmstudio_server_status(lms_path)
        if status is not None and status.get("running"):
            return True
        time.sleep(_LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS)
        elapsed += _LMSTUDIO_SERVER_START_POLL_INTERVAL_SECONDS
    return False


def _stop_lmstudio_server(lms_path: str) -> None:
    """Best-effort `lms server stop`. Only ever called for a server this
    module started itself; failures here must never surface as an install
    error."""
    try:
        subprocess.run([lms_path, "server", "stop"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _lmstudio_list_models(lms_path: str, timeout: float = 15) -> list[dict] | None:
    """List models `lms` currently sees on disk. None on any failure."""
    try:
        result = subprocess.run(
            [lms_path, "ls", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _lmstudio_find_model_key(models: list[dict], publisher: str, repo: str, filename: str) -> str | None:
    """Match logic behind `_lmstudio_model_key`, factored out so a caller
    that already has a fetched `lms ls --json` list can reuse it instead
    of triggering a second `lms` subprocess call for the same lookup."""
    expected = f"{publisher}/{repo}/{filename}"
    for entry in models:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.replace("\\", "/") == expected:
            key = entry.get("modelKey")
            return key if isinstance(key, str) else None
    return None


def _lmstudio_model_key(lms_path: str, publisher: str, repo: str, filename: str) -> str | None:
    """Resolve the modelKey LM Studio's API and `lms load`/`lms unload`
    actually expect for a just-linked model, by matching the on-disk path
    link_lmstudio placed it at against `lms ls --json`'s own path field.
    Never guess this from the publisher/repo folder name directly -
    confirmed against a real LM Studio 0.4.20 instance that a repo folder
    ending in a quantization suffix matching the GGUF's own detected
    quantization (e.g. `tinyllama-1.1b-chat-v1.0.Q4_K_M`) gets that suffix
    stripped in modelKey (`tinyllama-1.1b-chat-v1.0`) - the two are not
    interchangeable."""
    models = _lmstudio_list_models(lms_path)
    if models is None:
        return None
    return _lmstudio_find_model_key(models, publisher, repo, filename)


_LMSTUDIO_PROBE_TIMEOUT_SECONDS = 120
_LMSTUDIO_PROBE_PROMPT = "Reply with the single word OK."
_LMSTUDIO_PROBE_MAX_TOKENS = 8


def _probe_lmstudio_generate(
    port: int, model_key: str, timeout: float = _LMSTUDIO_PROBE_TIMEOUT_SECONDS
) -> bool | None:
    """Send a fixed short prompt to LM Studio's OpenAI-compatible endpoint,
    which JIT-loads `model_key` if it isn't already resident - confirmed
    against a real LM Studio 0.4.20 instance (a symlinked GGUF answered a
    /v1/chat/completions request for its resolved modelKey with no
    explicit `lms load` first). True on a real text response, False on an
    HTTP/response-shape failure (model didn't load), None on a network
    error - inconclusive, not proof of failure. `timeout` is generous
    because first-load time on a large model, not just generation time, is
    included."""
    import requests

    try:
        response = requests.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "model": model_key,
                "messages": [{"role": "user", "content": _LMSTUDIO_PROBE_PROMPT}],
                "max_tokens": _LMSTUDIO_PROBE_MAX_TOKENS,
                "stream": False,
            },
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, str) and len(content.strip()) > 0


def _lms_unload(lms_path: str, model_key: str) -> bool:
    """Best-effort isolation cleanup after a probe, mirroring
    quality.unload_model's role for Ollama. Confirmed against a real LM
    Studio instance that unloading a not-currently-loaded identifier exits
    cleanly rather than raising, but this still never propagates a
    failure either way."""
    try:
        result = subprocess.run([lms_path, "unload", model_key], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def verify_lmstudio_load(gguf_path: Path, repo_id: str | None) -> bool | None:
    """Prove a just-linked LM Studio model actually loads. Called once per
    successful link_lmstudio - LM Studio has no benchmark path to exercise
    this later the way Ollama's does. Soft-fails everywhere: only a
    confirmed bad generation returns False; every other obstacle returns
    None so a caller never turns "couldn't check" into "definitely
    broken."""
    publisher, repo = _lmstudio_publisher_repo(repo_id, gguf_path.name)
    lms_path = _lms_cli_path()
    if lms_path is None:
        return None
    status = _lmstudio_server_status(lms_path)
    if status is None:
        return None
    model_key = _lmstudio_model_key(lms_path, publisher, repo, gguf_path.name)
    if model_key is None:
        return None

    started_by_us = False
    if not status["running"]:
        if not _start_lmstudio_server(lms_path):
            return None
        started_by_us = True
        # The server may select a different port when it starts (for
        # example because the configured port became occupied after the
        # earlier stopped-status query). Probe the live status rather than
        # reusing stale pre-start metadata.
        status = _lmstudio_server_status(lms_path)
        if status is None or not status["running"]:
            _stop_lmstudio_server(lms_path)
            return None

    try:
        return _probe_lmstudio_generate(status["port"], model_key)
    finally:
        _lms_unload(lms_path, model_key)
        if started_by_us:
            _stop_lmstudio_server(lms_path)


# --- Ollama ------------------------------------------------------------


def sanitize_ollama_tag(filename: str) -> str:
    """Ollama model names must be lowercase [a-z0-9._-]."""
    name = filename
    if name.lower().endswith(".gguf"):
        name = name[: -len(".gguf")]
    name = name.lower()
    tag = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-")
    if tag in {"", ".", ".."}:
        tag = f"model-{hashlib.sha256(filename.encode()).hexdigest()[:12]}"
    return tag


def validate_ollama_tag(model_name: str) -> str:
    """Validate one local Ollama library name before constructing paths."""
    if (
        not isinstance(model_name, str)
        or len(model_name) > 200
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name) is None
        or model_name in {".", ".."}
    ):
        raise LinkError("Unsafe Ollama model name.")
    return model_name


def _ollama_runtime_name_from_manifest_path(
    manifest_path: Path, manifests_root: Path
) -> str | None:
    """Translate an Ollama manifest path into the name exposed by its API."""
    try:
        parts = manifest_path.relative_to(manifests_root).parts
    except ValueError:
        return None
    if len(parts) < 4:
        return None

    registry_name, namespace, *model_and_tag = parts
    model_parts = model_and_tag[:-1]
    tag = model_and_tag[-1]
    if not registry_name or not namespace or not model_parts or not tag:
        return None

    if registry_name == "registry.ollama.ai":
        prefix = [] if namespace == "library" else [namespace]
    else:
        prefix = [registry_name, namespace]
    model_name = "/".join([*prefix, *model_parts])
    return f"{model_name}:{tag}" if model_name else None


def _ollama_manifest_digest_index(models_dir: Path) -> dict[str, tuple[str, ...]]:
    """Walk the Ollama manifest tree once, bucketing runtime names by the
    sha256 hex digest of their model-layer blob.

    Shared by `_ollama_runtime_names_for_digest` (one digest) and
    `resolve_ollama_runtime_names_batch` (many entries at once) so resolving
    N models never re-walks the whole manifest tree N times (see issue
    #181's O(models) full-rescan cost).
    """
    manifests_root = models_dir / "manifests"
    if not manifests_root.is_dir():
        return {}

    index: dict[str, set[str]] = {}
    for manifest_path in manifests_root.rglob("*"):
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        layers = manifest.get("layers") if isinstance(manifest, dict) else None
        if not isinstance(layers, list):
            continue
        digest_hexes: set[str] = set()
        for layer in layers:
            if not (
                isinstance(layer, dict)
                and layer.get("mediaType") == "application/vnd.ollama.image.model"
            ):
                continue
            digest = str(layer.get("digest", "")).casefold()
            if digest.startswith("sha256:"):
                digest_hexes.add(digest.removeprefix("sha256:"))
        if not digest_hexes:
            continue
        runtime_name = _ollama_runtime_name_from_manifest_path(
            manifest_path, manifests_root
        )
        if not runtime_name:
            continue
        for digest_hex in digest_hexes:
            index.setdefault(digest_hex, set()).add(runtime_name)
    return {digest_hex: tuple(sorted(names)) for digest_hex, names in index.items()}


def _ollama_runtime_names_for_digest(
    model_sha256: object, *, models_dir: Path | None = None
) -> tuple[str, ...]:
    """Return exact Ollama API names whose model layer has this GGUF digest."""
    if not isinstance(model_sha256, str):
        return ()
    digest_hex = model_sha256.strip().casefold()
    if digest_hex.startswith("sha256:"):
        digest_hex = digest_hex.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", digest_hex) is None:
        return ()
    if models_dir is None:
        models_dir = ollama_models_dir()
    return _ollama_manifest_digest_index(models_dir).get(digest_hex, ())


def _resolve_ollama_runtime_name_impl(
    filename: str,
    entry: dict,
    get_candidates: Callable[[object], tuple[str, ...]],
) -> str:
    explicit = entry.get("ollama_runtime_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    link_name = entry.get("ollama_name")
    if not isinstance(link_name, str) or not link_name.strip():
        link_name = sanitize_ollama_tag(filename)
    else:
        link_name = link_name.strip()

    candidates = get_candidates(entry.get("sha256"))
    matching_link_name = tuple(
        candidate
        for candidate in candidates
        if sanitize_ollama_tag(candidate) == link_name.casefold()
    )
    if len(matching_link_name) == 1:
        return matching_link_name[0]
    if len(candidates) == 1:
        return candidates[0]
    return link_name


def resolve_ollama_runtime_name(
    filename: str, entry: dict, *, models_dir: Path | None = None
) -> str:
    """Resolve the exact Ollama API tag without guessing colon placement.

    Imported models historically stored only a filename-safe ``ollama_name``
    (for example ``qwen3-4b``) even when Ollama exposed ``qwen3:4b``. Prefer
    the exact tag recorded by new imports. For legacy entries, match the
    registry's GGUF SHA-256 against Ollama manifests; the digest avoids an
    unsafe hyphen-to-colon heuristic.
    """
    return _resolve_ollama_runtime_name_impl(
        filename,
        entry,
        lambda sha256: _ollama_runtime_names_for_digest(sha256, models_dir=models_dir),
    )


def resolve_ollama_runtime_names_batch(
    entries: Sequence[tuple[str, dict]], *, models_dir: Path | None = None
) -> dict[str, str]:
    """Batch form of `resolve_ollama_runtime_name` for callers resolving many
    registry entries at once (e.g. `omm benchmark all`'s memory-guard
    pre-check). Walks the Ollama manifest tree at most once instead of once
    per entry that lacks a cached `ollama_runtime_name` (see issue #181).
    """
    if models_dir is None:
        models_dir = ollama_models_dir()
    entries = list(entries)

    def _has_explicit(entry: dict) -> bool:
        value = entry.get("ollama_runtime_name")
        return isinstance(value, str) and bool(value.strip())

    needs_scan = any(not _has_explicit(entry) for _filename, entry in entries)
    digest_index = _ollama_manifest_digest_index(models_dir) if needs_scan else {}

    def get_candidates(model_sha256: object) -> tuple[str, ...]:
        if not isinstance(model_sha256, str):
            return ()
        digest_hex = model_sha256.strip().casefold()
        if digest_hex.startswith("sha256:"):
            digest_hex = digest_hex.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", digest_hex) is None:
            return ()
        return digest_index.get(digest_hex, ())

    return {
        filename: _resolve_ollama_runtime_name_impl(filename, entry, get_candidates)
        for filename, entry in entries
    }


def _guess_param_size(filename: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)[Bb](?:[-_.]|$)", filename)
    return f"{m.group(1)}B" if m else "unknown"


def _guess_quant(filename: str) -> str:
    m = re.search(r"(Q\d(?:_[A-Z0-9]+)*)", filename, re.IGNORECASE)
    return m.group(1).upper() if m else "unknown"


def _ollama_link_already_current(manifest_path: Path, gguf_path: Path, blobs_dir: Path) -> bool:
    """True if `manifest_path` is an omm-owned manifest for exactly this
    gguf_path whose model-layer blob is still the very same file - proven
    by inode identity (`samefile`), not a content hash. Re-linking would
    rewrite byte-identical output in this case, so callers can skip
    re-hashing a potentially multi-GB model on every repeat `omm link`
    when nothing has actually changed."""
    if not _owned_manifest(manifest_path, expected_source=gguf_path):
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    layers = manifest.get("layers") if isinstance(manifest, dict) else None
    if not isinstance(layers, list):
        return False
    digest = next(
        (
            layer.get("digest", "")
            for layer in layers
            if isinstance(layer, dict)
            and layer.get("mediaType") == "application/vnd.ollama.image.model"
        ),
        "",
    )
    if not digest.startswith("sha256:"):
        return False
    model_blob = blobs_dir / f"sha256-{digest.removeprefix('sha256:')}"
    try:
        return model_blob.samefile(gguf_path)
    except OSError:
        return False


def link_ollama(
    gguf_path: Path,
    model_name: str,
    models_dir: Path | None = None,
    verify_compat: bool = True,
    *,
    on_copy: CopyReporter | None = None,
    force: bool = False,
) -> bool:
    """Link a GGUF into one Ollama-format store as an atomic transaction."""
    transaction_models_dir = models_dir if models_dir is not None else ollama_models_dir()
    try:
        with locked(_models_transaction_lock(transaction_models_dir)):
            return _link_ollama_unlocked(
                gguf_path,
                model_name,
                models_dir,
                verify_compat,
                on_copy=on_copy,
                force=force,
            )
    except LinkError:
        raise
    except OSError as error:
        raise LinkError(f"Could not lock the Ollama model store: {error}") from error


def _link_ollama_unlocked(
    gguf_path: Path,
    model_name: str,
    models_dir: Path | None = None,
    verify_compat: bool = True,
    *,
    on_copy: CopyReporter | None = None,
    force: bool = False,
) -> bool:
    """Link into Ollama (or an Ollama-format engine at a different
    models_dir, e.g. AnythingLLM's bundled instance). Returns True if the
    source GGUF has an embedded chat template (Ollama reads it from the
    model blob at runtime), False if none was found and the caller should
    warn the user about it.

    `verify_compat` guards against Ollama's undocumented manifest format
    (see module docstring) drifting out from under omm's hand-rolled
    writer in a future Ollama release. Only meaningful for the real system
    Ollama, since it shells out to the `ollama` CLI, which always talks to
    the default daemon - callers linking into a different Ollama-format
    instance (e.g. AnythingLLM's bundled one) pass False. Also forced off
    whenever the caller passes an explicit models_dir (tests, or any
    non-default target) - the `ollama` CLI has no way to point at a
    specific models_dir, so verifying against it only makes sense when
    models_dir is the real live default the system daemon actually uses.
    """
    if models_dir is None:
        models_dir = ollama_models_dir()
    else:
        verify_compat = False
    model_name = validate_ollama_tag(model_name)

    try:
        gguf_meta = read_gguf_metadata(gguf_path, {"general.architecture", "tokenizer.chat_template"})
    except (OSError, ValueError, struct.error, KeyError) as e:
        raise LinkError(
            f"Could not read GGUF metadata from {gguf_path.name}: corrupted or truncated file ({e})."
        ) from e
    architecture = gguf_meta.get("general.architecture", "unknown")
    has_chat_template = "tokenizer.chat_template" in gguf_meta

    if architecture == "clip":
        # A CLIP-architecture GGUF is a multimodal projector (mmproj), not a
        # standalone text-generation model - it has no tokenizer/vocabulary
        # of its own and must be paired with its base model. Ollama's
        # llama-server crashes with "unsupported model architecture: 'clip'"
        # if asked to run it alone, so refuse the link instead of producing
        # a manifest that looks installed but can never generate text.
        raise LinkError(
            "This GGUF is a multimodal projector (mmproj), not a standalone "
            "model - it can't run alone in Ollama, so omm won't link it there."
        )

    ollama_version = _ollama_cli_version() if verify_compat else None
    if ollama_version is not None and _manifest_format_known_good(ollama_version) is False:
        # A previous link already found this Ollama version rejects omm's
        # hand-rolled manifest shape - skip straight to the slower-but-
        # correct native path instead of hashing the whole file and writing
        # a manifest that's just going to be thrown away.
        _fallback_to_native_create_under_model_lock(gguf_path, model_name, models_dir)
        return True

    if ollama_version is None or _manifest_format_known_good(ollama_version) is True:
        manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / model_name / "latest"
        if _ollama_link_already_current(manifest_path, gguf_path, models_dir / "blobs"):
            return has_chat_template

    model_sha256 = sha256_file(gguf_path)
    model_digest = f"sha256:{model_sha256}"

    blobs_dir = models_dir / "blobs"
    try:
        blobs_dir.mkdir(parents=True, exist_ok=True)

        model_blob = blobs_dir / f"sha256-{model_sha256}"
        # A matching content-addressed blob may be owned by Ollama or another
        # manifest. It is already usable; never replace it. Locked so two
        # omm processes linking the same model concurrently serialize on
        # the check-then-create instead of both reaching the `else` branch
        # and racing `link_file`'s own exists check, which used to surface
        # as a raw FileExistsError from the loser's symlink_to call.
        with locked(_engine_path_lock(model_blob)):
            if model_blob.exists():
                try:
                    blob_matches = model_blob.samefile(gguf_path) or (
                        sha256_file(model_blob) == model_sha256
                    )
                except OSError:
                    blob_matches = False
                if not blob_matches:
                    raise LinkError(
                        f"Existing Ollama model blob does not match its digest: {model_blob}."
                    )
            elif model_blob.is_symlink():
                raise LinkError(f"Refusing broken Ollama model blob symlink: {model_blob}.")
            else:
                link_file(gguf_path, model_blob, on_copy=on_copy)

        # Mirrors the config produced by `ollama create` for a bare GGUF (no
        # Modelfile TEMPLATE override): a single model layer, config mediaType
        # "application/vnd.docker.container.image.v1+json", and model_family
        # taken from the GGUF's own general.architecture field. With this shape,
        # Ollama reads tokenizer.chat_template straight from the GGUF at
        # inference time - no separate template layer is needed or created by
        # `ollama create` itself in this case.
        config = {
            "model_format": "gguf",
            "model_family": architecture,
            "model_families": [architecture],
            "model_type": _guess_param_size(gguf_path.name),
            "file_type": _guess_quant(gguf_path.name),
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [model_digest]},
        }
        config_bytes = json.dumps(config).encode()
        config_sha256 = hashlib.sha256(config_bytes).hexdigest()
        config_blob = blobs_dir / f"sha256-{config_sha256}"
        # Config blobs are shared across model names and content-addressed.
        # Serialize the check/create just like the model blob above; two
        # concurrent links with the same config used to see "missing" and
        # interleave non-atomic write_bytes calls.
        with locked(_engine_path_lock(config_blob)):
            if config_blob.exists():
                try:
                    config_matches = config_blob.read_bytes() == config_bytes
                except OSError:
                    config_matches = False
                if not config_matches:
                    raise LinkError(
                        f"Existing Ollama config blob does not match its digest: {config_blob}."
                    )
            elif config_blob.is_symlink():
                raise LinkError(f"Refusing broken Ollama config blob symlink: {config_blob}.")
            else:
                atomic_write_bytes(config_blob, config_bytes)
                try:
                    _record_ownership(config_blob, None, "copy")
                except Exception:
                    config_blob.unlink(missing_ok=True)
                    raise
            # Config blobs contain no credentials. A systemd Ollama daemon
            # commonly runs as a different user and must be able to read both
            # newly-written and previously-cached matching blobs.
            if platform.system() != "Windows":
                config_blob.chmod(0o644)

        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": f"sha256:{config_sha256}",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.ollama.image.model",
                    "digest": model_digest,
                    "size": gguf_path.stat().st_size,
                }
            ],
        }

        manifest_root = models_dir / "manifests" / "registry.ollama.ai" / "library"
        manifest_root.mkdir(parents=True, exist_ok=True)
        manifest_dir = manifest_root / model_name
        if manifest_dir.is_symlink():
            raise LinkError(f"Refusing Ollama symlinked manifest directory: {manifest_dir}.")
        if not manifest_dir.resolve().is_relative_to(manifest_root.resolve()):
            raise LinkError("Refusing Ollama manifest path outside the models directory.")
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "latest"
        manifest_json = json.dumps(manifest, indent=2)
        # Locked + read-back verified: two omm processes linking the same
        # model_name concurrently used to be able to interleave unlocked
        # writes and tear the manifest, which `ollama show` would then
        # reject and misattribute to a genuine version incompatibility,
        # permanently poisoning `_manifest_format_known_good` for a
        # transient race rather than a real format drift.
        with locked(_engine_path_lock(manifest_path)):
            if manifest_path.exists() or manifest_path.is_symlink():
                owned = _owned_manifest(manifest_path, expected_source=gguf_path)
                if not owned:
                    # Mirrors unlink_ollama's fallback: an omm-written manifest
                    # whose ownership record was lost (registry file corrupted
                    # and reset, or created before the registry existed) would
                    # otherwise permanently fail every re-link of this exact
                    # model - install reports success but leaves the stale
                    # manifest in place, and later `omm uninstall` also skips
                    # it because `linked.ollama` never turns True. If the
                    # existing manifest's model layer already hashes to the
                    # same content this call is about to write, it's
                    # unambiguously safe to replace regardless of the missing
                    # ownership record.
                    try:
                        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        existing_manifest = {}
                    owned = _manifest_model_layer_sha256(existing_manifest) == model_sha256
                if not owned:
                    raise LinkError(f"Refusing to replace unowned Ollama manifest at {manifest_path}.")
                manifest_path.unlink()
                _update_link_ownership(manifest_path, None)
            atomic_write_text(manifest_path, manifest_json)
            # atomic_write_text's tempfile.mkstemp() defaults to mode 0600
            # (owner read/write only), which os.replace() carries straight
            # through to the final path. Invisible on a single-user desktop
            # where the writer and Ollama's daemon are the same account, but
            # under a systemd-managed install (issue #117) the daemon runs
            # as its own dedicated user - confirmed live in CI, it gets a
            # flat "permission denied" opening this exact file and the
            # model never appears in `ollama list`, no matter how long you
            # wait. Ollama's own writes aren't secret, so match what
            # `ollama create` itself would leave behind.
            if platform.system() != "Windows":
                manifest_path.chmod(0o644)
            try:
                written_back = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                manifest_path.unlink(missing_ok=True)
                raise LinkError(
                    f"Ollama manifest for {model_name} did not read back intact after write: {e}."
                ) from e
            if written_back != manifest:
                manifest_path.unlink(missing_ok=True)
                raise LinkError(
                    f"Ollama manifest for {model_name} was corrupted during write (concurrent writer?)."
                )
            try:
                _record_ownership(manifest_path, gguf_path, "manifest")
            except OSError:
                manifest_path.unlink(missing_ok=True)
                raise
    except PermissionError as e:
        # A systemd-managed Ollama's models dir can be owned by a different
        # system user (issue #117) - the path may be correctly resolved yet
        # still unwritable by this process. Only the real default daemon has
        # a native-create escape hatch (see verify_compat's docstring); an
        # explicit models_dir has nowhere else to go but a plain LinkError.
        if not verify_compat:
            raise LinkError(
                f"Could not link {model_name} into Ollama: {e}",
                fix=(
                    f"Check that {models_dir} is writable by your user account, "
                    "or that Ollama isn't running as a different system user."
                ),
            ) from e
        _fallback_to_native_create_under_model_lock(gguf_path, model_name, models_dir)
        return True
    except OSError as e:
        raise LinkError(f"Could not link {model_name} into Ollama: {e}") from e

    if ollama_version is not None:
        has_chat_template = _ensure_ollama_accepts(
            gguf_path, model_name, models_dir, has_chat_template, ollama_version
        )

    return has_chat_template


def _ollama_manifest_compat_cache_path() -> Path:
    return config.OMM_HOME / "ollama_manifest_compat.json"


def _ollama_cli_version() -> str | None:
    """Raw `ollama --version` output, used only as a cache key so the
    compatibility probe below runs once per Ollama upgrade instead of on
    every link. Works even with no daemon running (~15-30ms observed,
    confirmed live) - it's a pure CLI query, not a network round trip."""
    exe = find_ollama_executable()
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout + result.stderr).strip() or None


def _manifest_format_known_good(ollama_version: str) -> bool | None:
    """True/False if this exact Ollama version was already probed; None if
    unknown (never checked, or the cache is for a different version - an
    Ollama upgrade can change the manifest shape)."""
    path = _ollama_manifest_compat_cache_path()
    if not path.exists():
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict):
        return None
    if cache.get("ollama_version") != ollama_version:
        return None
    compatible = cache.get("compatible")
    return compatible if isinstance(compatible, bool) else None


def _record_manifest_format_result(ollama_version: str, compatible: bool) -> None:
    path = _ollama_manifest_compat_cache_path()
    content = json.dumps({"ollama_version": ollama_version, "compatible": compatible})
    try:
        with locked(path):
            atomic_write_text(path, content)
    except OSError:
        pass


def _ollama_accepts_manifest(model_name: str) -> bool | None:
    """Ask the real `ollama` CLI to read back a manifest omm just wrote.
    True if Ollama parses it fine, False if it rejects/can't find it (our
    hand-rolled shape has drifted from what this Ollama version expects),
    None if the daemon isn't reachable at all - nothing to compare against,
    not a format problem."""
    exe = find_ollama_executable()
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "show", model_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if "could not connect" in result.stderr.lower():
        return None
    return False


def _fallback_to_native_create(gguf_path: Path, model_name: str, models_dir: Path) -> None:
    """Serialize a directly requested native import with link/unlink work."""
    try:
        with locked(_models_transaction_lock(models_dir)):
            _fallback_to_native_create_under_model_lock(gguf_path, model_name, models_dir)
    except LinkError:
        raise
    except OSError as error:
        raise LinkError(f"Could not lock the Ollama model store: {error}") from error


def _fallback_to_native_create_under_model_lock(
    gguf_path: Path, model_name: str, models_dir: Path
) -> None:
    """Serialize OMM-owned native imports while caller holds the store lock."""
    transaction_lock = _engine_path_lock(models_dir / ".omm-native-create")
    with locked(transaction_lock):
        _fallback_to_native_create_unlocked(gguf_path, model_name, models_dir)


def _fallback_to_native_create_unlocked(
    gguf_path: Path, model_name: str, models_dir: Path
) -> None:
    """Let the real `ollama` binary write its own manifest/blobs for this
    model when omm's hand-rolled shape has drifted out of sync with what
    this Ollama version expects. Confirmed empirically that `ollama create`
    re-parses and rewrites the model layer under a fresh content-addressed
    digest rather than reusing the source file's own hash, so this is a
    real byte copy (not zero-duplication) - acceptable here because it only
    runs after the fast path has already been confirmed broken, not on
    every link.

    Callers may reach this before ever writing (and ownership-recording) a
    manifest of their own - e.g. the already-known-bad short-circuit in
    link_ollama skips the hand-rolled write entirely - so this records
    ownership of whatever `ollama create` produces itself, unconditionally.
    Without this, `unlink_ollama`/`autoremove_ollama` would treat the
    resulting manifest as unowned and silently refuse to ever clean it up.
    """
    exe = find_ollama_executable()
    if exe is None:
        raise LinkError(
            f"Ollama's manifest format has changed and omm's link for {model_name} no "
            "longer works, but the `ollama` binary isn't on PATH to regenerate it natively."
        )
    manifest_path = (
        models_dir / "manifests" / "registry.ollama.ai" / "library" / model_name / "latest"
    )
    if manifest_path.exists() or manifest_path.is_symlink():
        if not _owned_manifest(manifest_path):
            raise LinkError(f"Refusing to replace unowned Ollama manifest at {manifest_path}.")

    source_size = gguf_path.stat().st_size
    required = source_size + disk_safety_reserve(source_size)
    try:
        free_bytes = shutil.disk_usage(disk_usage_path(models_dir)).free
    except OSError as error:
        raise LinkError(
            f"Could not verify free space before asking Ollama to import {model_name}: {error}."
        ) from error
    if free_bytes < required:
        raise InsufficientLinkSpaceError(
            f"Ollama must create a real copy of {model_name}, but its model volume has only "
            f"{free_bytes / 1024**3:.1f} GiB free; it needs at least "
            f"{required / 1024**3:.1f} GiB including safety space."
        )

    # Capacity is proven before replacing a currently working omm manifest.
    # A failed preflight must never destroy the user's existing Ollama model.
    if manifest_path.exists() or manifest_path.is_symlink():
        _unlink_ollama_unlocked(model_name, models_dir=models_dir)

    blobs_dir = models_dir / "blobs"
    try:
        blobs_dir.mkdir(parents=True, exist_ok=True)
        before_blobs = {path.name for path in blobs_dir.iterdir() if path.is_file()}
    except OSError:
        # A systemd-managed daemon (issue #117) may own this directory
        # without granting this process read/list access to it - `ollama
        # create` still works since the daemon does its own writing, we
        # just can't tell pre-existing blobs from new ones for
        # rollback-on-failure cleanup below.
        before_blobs = None

    def transaction_blobs() -> list[Path]:
        """New blobs referenced by this tag's manifest, never unrelated files.

        The Ollama daemon is not covered by OMM's file lock. A concurrent user
        pull can therefore add blobs while `ollama create` runs; treating every
        new filename as ours would record or delete the user's transaction.
        """
        if before_blobs is None:
            return []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            referenced = _manifest_blob_digests(manifest)
            return [
                path
                for path in blobs_dir.iterdir()
                if path.is_file()
                and path.name not in before_blobs
                and path.name in referenced
            ]
        except (OSError, ValueError):
            return []

    def cleanup_transaction() -> None:
        blobs_to_remove = transaction_blobs()
        try:
            with locked(_engine_path_lock(manifest_path)):
                manifest_path.unlink(missing_ok=True)
                _update_link_ownership(manifest_path, None)
        except OSError:
            pass
        for blob in blobs_to_remove:
            try:
                blob.unlink(missing_ok=True)
            except OSError:
                pass

    with tempfile.TemporaryDirectory() as tmp:
        modelfile = Path(tmp) / "Modelfile"
        modelfile.write_text(f"FROM {gguf_path}\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [exe, "create", model_name, "-f", str(modelfile)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            cleanup_transaction()
            if isinstance(e, OSError) and _is_disk_full_error(e):
                raise InsufficientLinkSpaceError(
                    f"Ollama ran out of disk space while importing {model_name}. "
                    "New transaction files were removed."
                ) from e
            raise LinkError(f"Could not regenerate Ollama manifest for {model_name}: {e}") from e
    new_blobs = transaction_blobs()
    if result.returncode != 0:
        # The native importer is not transactional. Remove only files that
        # appeared during this omm-owned invocation, never pre-existing user
        # blobs. This is especially important after ENOSPC.
        cleanup_transaction()
        stderr = result.stderr.strip()
        if "no space left" in stderr.lower() or "disk full" in stderr.lower():
            raise InsufficientLinkSpaceError(
                f"Ollama ran out of disk space while importing {model_name}. "
                "New transaction files were removed."
            )
        raise LinkError(
            f"Ollama rejected {model_name} even via native `ollama create`: {stderr}"
        )
    try:
        _record_ownership(manifest_path, None, "manifest")
        for blob in new_blobs:
            _record_ownership(blob, None, "copy")
    except OSError:
        pass


def _ensure_ollama_accepts(
    gguf_path: Path,
    model_name: str,
    models_dir: Path,
    has_chat_template: bool,
    ollama_version: str,
) -> bool:
    """Verify the manifest just written is one this Ollama version actually
    accepts, falling back to native `ollama create` if not. Cached per
    Ollama version, so the common case (already-confirmed-compatible) costs
    one dict lookup and no subprocess call at all."""
    cached = _manifest_format_known_good(ollama_version)
    if cached is True:
        return has_chat_template
    accepted = _ollama_accepts_manifest(model_name)
    if accepted is None:
        return has_chat_template
    _record_manifest_format_result(ollama_version, accepted)
    if accepted:
        return has_chat_template
    # Remove the rejected hand-written manifest and its omm-owned model
    # blob/copy before native import. Otherwise cross-volume installs can
    # briefly consume two extra full model copies and the rejected blob can
    # remain orphaned forever.
    _unlink_ollama_unlocked(model_name, models_dir=models_dir)
    _fallback_to_native_create_under_model_lock(gguf_path, model_name, models_dir)
    return True


def _manifest_blob_digests(manifest: dict) -> set[str]:
    if not isinstance(manifest, dict):
        return set()
    layers = manifest.get("layers")
    digests = set()
    if isinstance(layers, list):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            digest = layer.get("digest")
            if isinstance(digest, str) and digest:
                filename = digest.replace(":", "-")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", filename):
                    digests.add(filename)
    config = manifest.get("config")
    config_digest = config.get("digest") if isinstance(config, dict) else None
    if isinstance(config_digest, str) and config_digest:
        filename = config_digest.replace(":", "-")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", filename):
            digests.add(filename)
    return digests


def _manifest_model_layer_sha256(manifest: dict) -> str | None:
    if not isinstance(manifest, dict):
        return None
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        return None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        media_type = layer.get("mediaType")
        digest = layer.get("digest")
        if isinstance(media_type, str) and media_type.endswith("model") and isinstance(digest, str):
            return digest.removeprefix("sha256:")
    return None


def _unlink_ollama_manifest_only(
    model_name: str,
    models_dir: Path,
    expected_source: Path | None,
    expected_content_sha256: str | None,
) -> tuple[bool, set[str]]:
    """Delete one Ollama manifest (ownership-checked) without reclaiming any
    now-orphaned blob. Returns (removed, this manifest's model_digests) so a
    batch caller can reclaim blobs once across every manifest it deletes,
    instead of once per manifest (see `unlink_ollama_batch`)."""
    model_name = validate_ollama_tag(model_name)
    manifest_root = models_dir / "manifests" / "registry.ollama.ai" / "library"
    manifest_path = manifest_root / model_name / "latest"
    if not manifest_path.parent.resolve().is_relative_to(manifest_root.resolve()):
        return False, set()
    # Locked against link_ollama's own manifest_path lock: unlinking used to
    # run unlocked, so a concurrent `omm install`/`omm link` for the same
    # model_name could interleave with this delete - link_ollama's read-back
    # write finishes, records ownership, and returns success, while this
    # unlink (racing it unlocked) deletes that just-written manifest and
    # clears its ownership record right after, leaving the installer
    # believing the link succeeded when the manifest is actually gone.
    with locked(_engine_path_lock(manifest_path)):
        if not manifest_path.exists():
            return False, set()
        owned = _owned_manifest(manifest_path, expected_source=expected_source)
        if not owned and expected_content_sha256 is not None:
            try:
                manifest_for_check = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest_for_check = {}
            owned = _manifest_model_layer_sha256(manifest_for_check) == expected_content_sha256
        if not owned:
            return False, set()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            model_digests = _manifest_blob_digests(manifest)
        except (OSError, ValueError):
            model_digests = set()
        # Blobs are content-addressed and can be shared by a user manifest or
        # another omm model. Only remove an omm-owned blob after no remaining
        # manifest references its content digest.
        try:
            manifest_path.unlink()
        except OSError:
            return False, set()
        _update_link_ownership(manifest_path, None)
        try:
            manifest_path.parent.rmdir()
        except OSError:
            pass
    return True, model_digests


def _reclaim_ollama_blobs(models_dir: Path, model_digests: set[str]) -> None:
    """Free any of `model_digests` that no remaining manifest references.

    An omm-owned link/copy can be reclaimed once no remaining manifest
    references its content digest. This matters on Windows: deleting the
    hub filename alone does not free an NTFS hard link, and a loaded model
    may retain a handle until Ollama confirms it has unloaded.
    """
    if not model_digests:
        return
    manifests_root = models_dir / "manifests"
    referenced = set()
    if manifests_root.exists():
        for other in manifests_root.rglob("*"):
            if not other.is_file():
                continue
            try:
                data = json.loads(other.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            referenced.update(_manifest_blob_digests(data))
    for digest in model_digests - referenced:
        _unlink_owned_link_with_retry(models_dir / "blobs" / digest)


def unlink_ollama(
    model_name: str,
    models_dir: Path | None = None,
    expected_source: Path | None = None,
    expected_content_sha256: str | None = None,
) -> bool:
    """Remove an owned Ollama manifest and now-unreferenced owned blobs."""
    transaction_models_dir = models_dir if models_dir is not None else ollama_models_dir()
    try:
        with locked(_models_transaction_lock(transaction_models_dir)):
            return _unlink_ollama_unlocked(
                model_name,
                models_dir=models_dir,
                expected_source=expected_source,
                expected_content_sha256=expected_content_sha256,
            )
    except OSError:
        return False


def _unlink_ollama_unlocked(
    model_name: str,
    models_dir: Path | None = None,
    expected_source: Path | None = None,
    expected_content_sha256: str | None = None,
) -> bool:
    """Returns whether a manifest was actually removed.

    Ownership is normally proven by the link-ownership registry
    (`_owned_manifest`). That registry can be silent on a manifest omm
    genuinely created itself - installed before the registry existed, or
    a record lost some other way - in which case falling back to "no
    record, so do nothing" leaves the manifest (and its now-broken blob
    symlink once the caller deletes the hub source file right after this
    call returns) orphaned in Ollama forever, with no supported way for
    the user to remove it again: `omm remove` already forgot the model,
    and `omm list` no longer shows it. `expected_content_sha256` - the
    sha256 the omm registry entry itself recorded for this file at
    install time - gives a second, ownership-registry-independent proof:
    if the manifest's model layer digest matches it exactly, this is
    unambiguously the same model content omm is deleting, regardless of
    whether the ownership record survived."""
    if models_dir is None:
        models_dir = ollama_models_dir()
    removed, model_digests = _unlink_ollama_manifest_only(
        model_name, models_dir, expected_source, expected_content_sha256
    )
    if not removed:
        return False
    _reclaim_ollama_blobs(models_dir, model_digests)
    return True


def unlink_ollama_batch(
    specs: Sequence[tuple[str, Path | None, str | None]],
    models_dir: Path | None = None,
) -> dict[str, bool]:
    """Batch form of `unlink_ollama` for callers removing many models in one
    pass (e.g. `omm uninstall all`). Each manifest is still deleted and
    ownership-checked individually (and under its own lock), but the
    expensive blob-reclaim rescan of the whole manifest tree runs once at
    the end instead of once per model (see issue #181).

    `specs` is a sequence of ``(model_name, expected_source,
    expected_content_sha256)`` tuples, matching `unlink_ollama`'s per-model
    arguments. Returns a dict keyed by model_name (the last spec wins if a
    name repeats).
    """
    if models_dir is None:
        models_dir = ollama_models_dir()

    validated_specs = [
        (validate_ollama_tag(model_name), expected_source, expected_content_sha256)
        for model_name, expected_source, expected_content_sha256 in specs
    ]
    results: dict[str, bool] = {}
    try:
        with locked(_models_transaction_lock(models_dir)):
            all_digests: set[str] = set()
            for model_name, expected_source, expected_content_sha256 in validated_specs:
                removed, model_digests = _unlink_ollama_manifest_only(
                    model_name, models_dir, expected_source, expected_content_sha256
                )
                results[model_name] = removed
                if removed:
                    all_digests.update(model_digests)
            _reclaim_ollama_blobs(models_dir, all_digests)
            return results
    except (OSError, FileLockTimeout):
        for model_name, _, _ in validated_specs:
            results.setdefault(model_name, False)
        return results


# --- Autoremove (broken symlink cleanup) ------------------------------


def autoremove_lmstudio() -> int:
    """Delete broken LM Studio symlinks (source .gguf no longer exists).
    Returns the number removed."""
    base = lmstudio_models_dir()
    if not base.exists():
        return 0

    removed = 0
    # Loaded once and reused for every candidate below instead of having
    # unlink_owned_link's own ownership check reload and re-parse the whole
    # on-disk registry per broken symlink found in this (possibly large,
    # recursively-walked) tree.
    ownership = _load_link_ownership()
    for path in list(base.rglob("*")):
        if path.is_symlink() and not path.exists():
            if unlink_owned_link(path, record=ownership.get(_link_key(path))):
                removed += 1
                for parent in (path.parent, path.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
    return removed


def autoremove_ollama(models_dir: Path | None = None) -> tuple[int, int]:
    """Delete broken Ollama model-layer blob symlinks and any manifests
    that reference them. Returns (blobs_removed, manifests_removed).

    Neither half checks link ownership (see issue #171): a blob that is
    already a dangling symlink has no data left to lose by removing the
    dangling pointer itself, and a manifest whose model layer digest
    matches one of those already-confirmed-dangling blobs can never load
    regardless of who created it - the content is gone. Requiring an
    ownership record here (as most other unlink paths correctly do, to
    avoid touching a link/manifest they can't prove is theirs) only
    protected records that had already stopped existing: a manifest
    linked before the ownership registry existed, or one whose record
    was lost some other way, would sit in `ollama list`/`omm benchmark
    all` forever with a permanently broken blob and no supported way to
    remove it - `omm remove` had already forgotten it, and this command
    (the intended cleanup path for exactly this situation) silently
    declined to touch it either."""
    if models_dir is None:
        models_dir = ollama_models_dir()
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    if not blobs_dir.exists():
        return (0, 0)

    broken_digests = set()
    # Cleared together in one locked read-modify-write at the end instead of
    # once per removed blob/manifest - a run that finds many broken links at
    # once used to reload and rewrite the whole on-disk registry that many
    # times over for no observable difference in the end state.
    cleared_paths: list[Path] = []
    for blob in blobs_dir.iterdir():
        if blob.is_symlink() and not blob.exists():
            try:
                blob.unlink()
            except OSError:
                continue
            cleared_paths.append(blob)
            broken_digests.add(blob.name)

    manifests_removed = 0
    if broken_digests and manifests_root.exists():
        for manifest_path in list(manifests_root.rglob("latest")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            layer_digests = _manifest_blob_digests(manifest)
            if layer_digests & broken_digests:
                try:
                    manifest_path.unlink()
                except OSError:
                    continue
                cleared_paths.append(manifest_path)
                manifests_removed += 1
                try:
                    manifest_path.parent.rmdir()
                except OSError:
                    pass

    _bulk_clear_link_ownership(cleared_paths)
    return (len(broken_digests), manifests_removed)


# --- Per-OS app data directory (Electron/Tauri userData convention) --------


def _app_data_dir(product_name: str) -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / product_name
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / product_name
    return Path.home() / ".config" / product_name


# --- AnythingLLM (bundled Ollama) -------------------------------------------


def anythingllm_app_dir() -> Path:
    return _app_data_dir("anythingllm-desktop")


def anythingllm_ollama_models_dir() -> Path:
    return anythingllm_app_dir() / "storage" / "models" / "ollama"


# The Windows installer is electron-builder NSIS, whose per-user default
# install directory is %LOCALAPPDATA%\Programs\<productName>; the folder name
# has shipped under both the product name and the package name, so all the
# spellings are checked rather than betting on one.
_ANYTHINGLLM_WINDOWS_INSTALL_DIRS = (
    "AnythingLLM",
    "AnythingLLM Desktop",
    "anythingllm-desktop",
)
# Linux has no package-manager install: the official installer.sh unpacks the
# app into this fixed directory under $HOME.
_ANYTHINGLLM_LINUX_INSTALL_DIRS = [Path.home() / "AnythingLLMDesktop"]


def is_anythingllm_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        # The .app bundle is written at install time, so macOS never had the
        # first-run gap the other two platforms did.
        return _app_bundle_installed("AnythingLLM")
    # anythingllm_app_dir() is the Electron userData directory, which only
    # exists once AnythingLLM has been launched at least once. Keep it (it
    # catches installs in non-default locations), but OR it with the
    # install-time artifacts so an installed-but-never-launched app is not
    # reported as missing.
    if anythingllm_app_dir().exists():
        return True
    if system == "Windows":
        return _windows_install_artifact_exists(
            _ANYTHINGLLM_WINDOWS_INSTALL_DIRS, "AnythingLLM*.lnk"
        )
    return _linux_install_artifact_exists(
        _ANYTHINGLLM_LINUX_INSTALL_DIRS, "*nythingllm*.desktop"
    )


# --- Jan (llamacpp-extension, model.yml manifest) ---------------------------


def jan_app_dir() -> Path:
    return _app_data_dir("Jan")


def jan_models_dir() -> Path:
    return jan_app_dir() / "data" / "llamacpp" / "models"


def is_jan_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _app_bundle_installed("Jan")
    if jan_app_dir().exists():
        return True
    if system == "Linux" and shutil.which("flatpak") is not None:
        # jan_app_dir() (~/.config/Jan) is only created the first time Jan
        # actually launches - a flatpak install that succeeded but was
        # never run leaves nothing there yet, which used to make
        # install_engine("jan") report "still isn't detected" right after
        # a genuinely successful `flatpak install`. `flatpak info` checks
        # the package itself instead, mirroring what _app_bundle_installed
        # does for Darwin (confirmed against a real ubuntu-latest CI run).
        try:
            return subprocess.run(
                ["flatpak", "info", "ai.jan.Jan"], capture_output=True, timeout=10
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def _jan_model_yaml_path(model_id: str) -> Path:
    if (
        not model_id
        or model_id in {".", ".."}
        or "/" in model_id
        or "\\" in model_id
        or ":" in model_id
        or any(ord(character) < 32 for character in model_id)
    ):
        raise LinkError("Unsafe Jan model id.")
    return jan_models_dir() / model_id / "model.yml"


def link_jan(gguf_path: Path, model_id: str) -> Path:
    """Register `gguf_path` with Jan by writing a model.yml manifest that
    points model_path straight at it - no symlink needed, since Jan's own
    local-file import does the same (stores the absolute path as-is)."""
    config_path = _jan_model_yaml_path(model_id)
    try:
        # JSON string literals are valid YAML scalars and correctly escape
        # quotes/control characters in otherwise-valid local paths.
        content = (
            f"model_path: {json.dumps(str(gguf_path))}\n"
            f"name: {json.dumps(model_id)}\n"
            f"size_bytes: {gguf_path.stat().st_size}\n"
        )
        root = jan_models_dir()
        root.mkdir(parents=True, exist_ok=True)
        with locked(_engine_path_lock(config_path)):
            if config_path.parent.is_symlink():
                raise LinkError(
                    f"Refusing Jan symlinked model directory: {config_path.parent}."
                )
            if not config_path.parent.resolve().is_relative_to(root.resolve()):
                raise LinkError("Refusing Jan manifest path outside the models directory.")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists() and not _owned_manifest(
                config_path, expected_source=gguf_path
            ):
                raise LinkError(
                    f"Refusing to replace unowned Jan manifest at {config_path}."
                )
            atomic_write_text(config_path, content)
            try:
                _record_ownership(config_path, gguf_path, "manifest")
            except Exception:
                config_path.unlink(missing_ok=True)
                raise
    except OSError as e:
        raise LinkError(f"Could not write Jan manifest at {config_path}: {e}") from e
    return config_path


def unlink_jan(model_id: str, expected_source: Path | None = None) -> None:
    """See `unlink_ollama`'s docstring for why ownership is not treated as
    strictly required (issue #171): `_owned_manifest` alone silently no-ops
    when the link-ownership registry is lost/corrupted or the manifest
    predates it, orphaning the Jan model.yml forever. `expected_source` -
    the gguf path omm's own model registry recorded for this model - gives
    a second, ownership-registry-independent proof: if model.yml's own
    `model_path` field (read straight off disk, not from the possibly-lost
    registry) names exactly that source, this is unambiguously the same
    manifest omm created, regardless of whether the ownership record
    survived."""
    config_path = _jan_model_yaml_path(model_id)
    if not config_path.parent.resolve().is_relative_to(jan_models_dir().resolve()):
        return
    with locked(_engine_path_lock(config_path)):
        owned = _owned_manifest(config_path, expected_source=expected_source)
        if not owned and expected_source is not None and config_path.exists() and not config_path.is_symlink():
            recorded_path = read_jan_model_path(config_path)
            if recorded_path is not None and _link_key(Path(recorded_path)) == _link_key(expected_source):
                owned = True
        if owned:
            try:
                config_path.unlink()
                _update_link_ownership(config_path, None)
            except OSError:
                return
    try:
        config_path.parent.rmdir()
    except OSError:
        pass


_JAN_MODEL_PATH_RE = re.compile(r"^model_path:\s*(.*?)\s*$", re.MULTILINE)


def read_jan_model_path(config_path: Path) -> str | None:
    """Pull `model_path` out of a model.yml. Jan's manifest only ever needs
    this one field read back, so a tiny regex stands in for a full YAML
    parser rather than adding a new dependency for it."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _JAN_MODEL_PATH_RE.search(text)
    if match is None:
        return None
    value = match.group(1)
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, str) else None
    return value


def autoremove_jan() -> int:
    """Delete model.yml manifests whose model_path no longer points at an
    existing file. Returns the number removed.

    Ownership is normally proven by the link-ownership registry
    (`_owned_manifest`), same as elsewhere. But that registry can be silent
    on a manifest omm genuinely created itself - installed before the
    registry existed, or a record lost some other way - which used to leave
    the model.yml (and its dead `model_path`) orphaned in Jan forever, the
    same bug class fixed for Ollama in issue #171 (`autoremove_ollama`).

    Unlike Ollama's dangling blob symlink - which structurally can only
    have been created by omm, so the ownership check is safely skipped
    entirely there - a bare model.yml is exactly the same file Jan itself
    writes for a model the user imported directly, so a missing
    `model_path` alone is not proof the content is gone for good (it may
    simply be on unmounted removable/network media) and is not enough to
    drop the ownership requirement. `model_path` resolving inside omm's own
    `MODELS_DIR`, however, is the same kind of structural certainty Ollama's
    fix relies on: nothing but omm's own downloader ever puts a file there,
    so a manifest whose `model_path` was inside `MODELS_DIR` and has since
    disappeared is unambiguously an omm-created manifest whose source omm
    itself removed - safe to clean up without a surviving ownership record.
    A record-less broken manifest whose `model_path` points elsewhere is
    left alone, exactly as before.
    """
    models_dir = jan_models_dir()
    if not models_dir.exists():
        return 0
    removed = 0
    # Loaded once and reused for every candidate below instead of having
    # _owned_manifest reload and re-parse the whole on-disk registry per
    # model.yml checked in this directory.
    ownership = _load_link_ownership()
    models_root = MODELS_DIR.expanduser().resolve(strict=False)
    for config_path in list(models_dir.glob("*/model.yml")):
        if not config_path.parent.resolve().is_relative_to(models_dir.resolve()):
            continue
        model_path = read_jan_model_path(config_path)
        if not model_path or Path(model_path).exists():
            continue
        record = ownership.get(_link_key(config_path))
        owned = _owned_manifest(config_path, record=record)
        if not owned:
            resolved_model_path = Path(model_path).expanduser().resolve(strict=False)
            owned = resolved_model_path.is_relative_to(models_root)
        if not owned:
            continue
        try:
            config_path.unlink()
            _update_link_ownership(config_path, None)
        except OSError:
            continue
        removed += 1
        try:
            config_path.parent.rmdir()
        except OSError:
            pass
    return removed


# --- Msty (MstyStudio) -------------------------------------------------


def mstystudio_app_dir() -> Path:
    return _app_data_dir("MstyStudio")


def mstystudio_models_dir() -> Path:
    return mstystudio_app_dir() / "models"


# electron-builder NSIS per-user install folder names seen for Msty Studio.
_MSTYSTUDIO_WINDOWS_INSTALL_DIRS = ("Msty Studio", "MstyStudio")


def is_mstystudio_installed() -> bool:
    if platform.system() == "Darwin":
        return _app_bundle_installed("MstyStudio")
    if mstystudio_app_dir().exists():
        return True
    if platform.system() == "Windows":
        # Same installed-but-never-launched gap as AnythingLLM: the
        # userData dir only appears on first launch.
        return _windows_install_artifact_exists(_MSTYSTUDIO_WINDOWS_INSTALL_DIRS, "Msty*.lnk")
    return False


# --- KoboldCpp / text-generation-webui (no fixed install location) --------
#
# Neither ships an installer that lands in a standard OS app-data path.
# Only the OMM-owned engine directory is searched by default. Tests may
# inject additional roots explicitly, but legacy/common user directories are
# never automatic execution provenance.
_HEURISTIC_SEARCH_ROOTS: list[Path] = []


def engine_install_dir() -> Path:
    """Explicitly approved manual engine copies live under <OMM_HOME>/apps.

    Read through `config` at call time so the `isolated_omm_home` fixture
    and OMM_HOME both apply.
    """
    return config.OMM_HOME / "apps"


def _heuristic_search_roots() -> list[Path]:
    return [engine_install_dir(), *_HEURISTIC_SEARCH_ROOTS]


def _trusted_discovery_entry(entry: Path, root: Path) -> bool:
    try:
        return not entry.is_symlink() and entry.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


@lru_cache(maxsize=1)
def find_koboldcpp_binary() -> Path | None:
    for root in _heuristic_search_roots():
        if root.is_symlink():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if _trusted_discovery_entry(entry, root) and entry.is_file() and entry.name.lower().startswith("koboldcpp"):
                return entry
        for entry in entries:
            if _trusted_discovery_entry(entry, root) and entry.is_dir() and "koboldcpp" in entry.name.lower():
                try:
                    sub_entries = list(entry.iterdir())
                except OSError:
                    continue
                for sub in sub_entries:
                    if _trusted_discovery_entry(sub, entry) and sub.is_file() and sub.name.lower().startswith("koboldcpp"):
                        return sub
    return None


def is_koboldcpp_installed() -> bool:
    return find_koboldcpp_binary() is not None


def koboldcpp_models_dir() -> Path | None:
    """A `models` folder next to wherever the koboldcpp binary was found.
    KoboldCpp itself won't auto-scan this - the user still has to launch it
    with `--admindir` (or `--downloaddir`) pointed at this folder - but it
    gives every omm-linked model one well-known place to point that at."""
    binary = find_koboldcpp_binary()
    return binary.parent / "models" if binary is not None else None


_TEXTGENWEBUI_NAME_HINT = re.compile(
    r"text-generation-webui|oobabooga|textgen", re.IGNORECASE
)


@lru_cache(maxsize=1)
def find_textgenwebui_root() -> Path | None:
    for root in _heuristic_search_roots():
        if root.is_symlink():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not (
                _trusted_discovery_entry(entry, root)
                and entry.is_dir()
                and _TEXTGENWEBUI_NAME_HINT.search(entry.name)
            ):
                continue
            # Old git-clone install: server.py + one_click.py at the root.
            if (entry / "server.py").exists() and (entry / "one_click.py").exists():
                return entry
            # Portable prebuilt release: server.py lives under app/, and
            # there's no one_click.py at all (verified against a real
            # release archive, not the docs).
            if (entry / "app" / "server.py").exists():
                return entry
    return None


def is_textgenwebui_installed() -> bool:
    return find_textgenwebui_root() is not None


def textgenwebui_models_dir() -> Path | None:
    root = find_textgenwebui_root()
    return root / "user_data" / "models" if root is not None else None


# --- Engine dispatch table --------------------------------------------------
#
# Ties every engine above together behind one uniform interface so cli.py
# can loop over `ENGINES` instead of hardcoding each app by name.


@dataclass(frozen=True)
class EngineSpec:
    key: str
    label: str


@dataclass(frozen=True)
class DiskCopyRisk:
    path: Path
    engine: str
    reason: str


ENGINES: list[EngineSpec] = [
    EngineSpec("ollama", "Ollama"),
    EngineSpec("lmstudio", "LM Studio"),
    EngineSpec("jan", "Jan"),
    EngineSpec("anythingllm", "AnythingLLM"),
    EngineSpec("mstystudio", "Msty"),
    EngineSpec("textgenwebui", "text-generation-webui"),
    EngineSpec("koboldcpp", "KoboldCpp"),
]

def is_engine_installed(key: str) -> bool:
    # Deliberately an if/elif calling each is_X_installed() by name (not a
    # dict of function references captured at import time) so that tests
    # monkeypatching e.g. `linker.is_ollama_installed` still take effect -
    # a dict built at module load time would freeze in the original
    # function object instead.
    if key == "ollama":
        return is_ollama_installed()
    if key == "lmstudio":
        return is_lmstudio_installed()
    if key == "jan":
        return is_jan_installed()
    if key == "anythingllm":
        return is_anythingllm_installed()
    if key == "mstystudio":
        return is_mstystudio_installed()
    if key == "textgenwebui":
        return is_textgenwebui_installed()
    if key == "koboldcpp":
        return is_koboldcpp_installed()
    raise ValueError(f"unknown engine: {key}")


@dataclass(frozen=True)
class EngineInstallResult:
    key: str
    status: str  # "installed" | "failed" | "unsupported_platform"
    message: str


def has_automated_installer(key: str) -> bool:
    """Single source of truth for "does install_engine() have a real branch
    for this engine key, on the CURRENT platform" - onboarding.py calls
    this instead of keeping its own separate set of automated engine keys,
    so a future engine added to one place without the other can't leave
    the wizard calling install_engine() on a key that just raises
    NotImplementedError. Deliberately mirrors install_engine()'s own
    if/elif shape (add one line here per new branch added there).

    Platform/arch-aware for the engines whose automation is not universal:
    mirrors the exact same checks their _install_<engine> function uses to
    decide unsupported_platform vs. actually attempting an install, so the
    onboarding checklist never labels an engine "(auto-install)" only for
    the actual attempt to fail with unsupported_platform. Deliberately
    cheap and side-effect-free (string/tuple comparisons only, no
    filesystem or network access) since it's called just to build labels,
    not to attempt anything."""
    if key == "ollama":
        return platform.system() in {"Darwin", "Windows"}
    if key == "lmstudio":
        return platform.system() in {"Darwin", "Windows"}
    if key == "jan":
        return platform.system() in {"Darwin", "Windows", "Linux"}
    if key == "anythingllm":
        # Brew-cask (Darwin) only. No flatpak/Linux path was ever built for
        # this one, and the Windows winget package is gone - see
        # _install_anythingllm for both. Windows used to be listed here,
        # which made the wizard attempt a winget install that could not
        # possibly succeed and then fall back to a manual URL; reporting it
        # unsupported shows that same guidance up front instead.
        return platform.system() == "Darwin"
    if key == "mstystudio":
        # Brew-cask only - no winget package targets the current app (see
        # _install_mstystudio) and no Linux package manager exists at all.
        return platform.system() == "Darwin"
    if key == "koboldcpp":
        return False
    if key == "textgenwebui":
        return False
    return False


def install_engine(
    key: str, *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Dispatch table mirroring is_engine_installed()'s if/elif style so
    individual branches stay monkeypatchable in tests. Every engine in
    ENGINES has a handler, but handlers without a verified package-manager
    route fail closed with manual-install guidance. A key not in ENGINES at
    all still raises NotImplementedError."""
    if key == "ollama":
        return _install_ollama(on_output=on_output)
    if key == "lmstudio":
        return _install_lmstudio(on_output=on_output)
    if key == "jan":
        return _install_jan(on_output=on_output)
    if key == "anythingllm":
        return _install_anythingllm(on_output=on_output)
    if key == "mstystudio":
        return _install_mstystudio(on_output=on_output)
    if key == "koboldcpp":
        return _install_koboldcpp(on_output=on_output)
    if key == "textgenwebui":
        return _install_textgenwebui(on_output=on_output)
    raise NotImplementedError(f"no automated installer for engine: {key}")


def _stream_subprocess(
    args: list[str] | str,
    on_output: Callable[[str], None] | None,
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, str] | None:
    """Runs args, streaming each stdout line to on_output as it arrives.
    Returns (returncode, None-marker) via the process wait(), or None if
    the process itself couldn't start (caller turns that into a result).
    `args` may be a pre-built command-line string for installers whose
    argument syntax Windows' argv re-quoting would break (NSIS /D=)."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            env=env,
        )
    except OSError:
        raise
    for line in proc.stdout:
        if on_output is not None:
            on_output(line.rstrip("\n"))
    return proc.wait()


_OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
_LMSTUDIO_DOWNLOAD_URL = "https://lmstudio.ai/download"


def _install_ollama(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    return _install_via_package_manager(
        key="ollama", label="Ollama", manual_url=_OLLAMA_DOWNLOAD_URL,
        is_installed=is_ollama_installed, on_output=on_output,
        brew_cask="ollama-app", winget_id="Ollama.Ollama",
    )


def _install_lmstudio(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Install LM Studio through the platform package manager only."""
    return _install_via_package_manager(
        key="lmstudio", label="LM Studio", manual_url=_LMSTUDIO_DOWNLOAD_URL,
        is_installed=is_lmstudio_installed, on_output=on_output,
        brew_cask="lm-studio", winget_id="ElementLabs.LMStudio",
    )


def _install_via_package_manager(
    *,
    key: str,
    label: str,
    manual_url: str,
    is_installed: Callable[[], bool],
    on_output: Callable[[str], None] | None = None,
    brew_cask: str | None = None,
    winget_id: str | None = None,
    flatpak_id: str | None = None,
) -> EngineInstallResult:
    """Shared shape for engines whose only automated path is a package
    manager: brew cask on macOS, winget on Windows, flatpak on Linux. Any
    platform without a configured option (a None kwarg, or the package
    manager itself missing) falls back to unsupported_platform with a
    manual link - never guesses a direct download URL."""
    system = platform.system()
    args: list[str] | None = None
    if system == "Darwin" and brew_cask is not None:
        if shutil.which("brew") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"Homebrew not found - install manually from {manual_url}"
            )
        args = ["brew", "install", "--cask", brew_cask]
    elif system == "Windows" and winget_id is not None:
        if shutil.which("winget") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"winget not found - install manually from {manual_url}"
            )
        args = [
            "winget",
            "install",
            "-e",
            "--id",
            winget_id,
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ]
    elif system == "Linux" and flatpak_id is not None:
        if shutil.which("flatpak") is None:
            return EngineInstallResult(
                key, "unsupported_platform", f"flatpak not found - install manually from {manual_url}"
            )
        args = ["flatpak", "install", "-y", "flathub", flatpak_id]
    else:
        return EngineInstallResult(
            key, "unsupported_platform", f"No automated installer for {system} - install manually from {manual_url}"
        )

    try:
        returncode = _stream_subprocess(args, on_output)
    except OSError as e:
        return EngineInstallResult(key, "failed", f"Could not start installer: {e}")

    if is_installed():
        return EngineInstallResult(key, "installed", f"{label} installed successfully.")

    # brew refuses to touch a cask it already has a receipt for ("already
    # installed", no-op, exit 0) even when the app bundle itself is gone -
    # e.g. the user dragged it to the Trash instead of `brew uninstall
    # --cask`. is_installed() (a real /Applications bundle check) still
    # says no, so retry with `brew reinstall --cask`, which uninstalls and
    # reinstalls regardless of the receipt's version. `install --cask
    # --force` was tried first but does NOT fix this: confirmed against a
    # real Homebrew 6.0.18 that a Trashed AnythingLLM.app still reports
    # "Not upgrading anythingllm, the latest version is already installed"
    # and exits 0 with `--force` too, leaving the Caskroom symlink pointing
    # at nothing - only `reinstall` actually re-moves the .app back into
    # /Applications. Cheap when the cask really was already fully
    # installed (skips the same no-op path); only case that matters is
    # this stale-receipt one.
    if (
        returncode == 0
        and system == "Darwin"
        and args is not None
        and args[:3] == ["brew", "install", "--cask"]
    ):
        try:
            returncode = _stream_subprocess(["brew", "reinstall", "--cask", brew_cask], on_output)
        except OSError as e:
            return EngineInstallResult(key, "failed", f"Could not start installer: {e}")
        if is_installed():
            return EngineInstallResult(key, "installed", f"{label} installed successfully.")

    detail = f" (installer exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        key,
        "failed",
        f"Installer ran but {label} still isn't detected{detail}. Install manually from {manual_url}",
    )


def _install_jan(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    return _install_via_package_manager(
        key="jan",
        label="Jan",
        manual_url="https://jan.ai/download",
        is_installed=is_jan_installed,
        on_output=on_output,
        brew_cask="jan",
        winget_id="Jan.Jan",
        flatpak_id="ai.jan.Jan",
    )


def _install_anythingllm(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    # Brew cask only. Both other platforms deliberately fall through to
    # unsupported_platform:
    #
    # Windows - there is no winget package any more. MintplexLabs.AnythingLLM
    # was removed from the community repo on 2025-02-18 by
    # microsoft/winget-pkgs#230632 ("New installer URL is behind captcha"),
    # and no manifest for this app exists under any publisher id today
    # (re-verified against microsoft/winget-pkgs and against a real
    # `winget search` on 2026-08-19: zero results, exit 0x8A150014). Keeping
    # the id here only bought an attempt that always failed. A direct
    # download of the vendor's AnythingLLMDesktop.exe was considered and
    # rejected: it is a ~396 MB NSIS installer with no vendor-documented
    # silent flag. (The second half of that argument - that detection would
    # miss a silent install because it keyed off the first-run-only Electron
    # userData directory - no longer holds: is_anythingllm_installed() now
    # also probes the install directory and Start Menu shortcut.)
    #
    # Linux - the only official install method is an interactive
    # installer.sh (sudo AppArmor-profile prompt, no documented silent
    # flag) - same risk class the original design excluded
    # text-generation-webui's git-clone path for.
    return _install_via_package_manager(
        key="anythingllm",
        label="AnythingLLM",
        manual_url="https://docs.anythingllm.com/installation-desktop/overview",
        is_installed=is_anythingllm_installed,
        on_output=on_output,
        brew_cask="anythingllm",
    )


def _install_mstystudio(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    # No winget_id/flatpak_id: the only winget entry for this app family
    # (CloudStack.Msty) targets the deprecated pre-rebrand "Msty" app, not
    # current "Msty Studio" - using it would install the wrong software.
    # No Linux package manager exists at all.
    return _install_via_package_manager(
        key="mstystudio",
        label="Msty",
        manual_url="https://msty.ai/products/studio/",
        is_installed=is_mstystudio_installed,
        on_output=on_output,
        brew_cask="mstystudio",
    )


def _install_koboldcpp(*, on_output: Callable[[str], None] | None = None) -> EngineInstallResult:
    return EngineInstallResult(
        "koboldcpp", "unsupported_platform",
        "Automatic installation is disabled because the upstream latest-download artifact has no pinned SHA-256. Install manually from https://github.com/LostRuins/koboldcpp/releases",
    )
_TEXTGENWEBUI_RELEASES_URL = "https://github.com/oobabooga/text-generation-webui/releases"

# The real release only ships one narrow ARM build (linux-arm64-cuda13.1) -
# not worth the complexity of supporting yet, but guessing an x86_64 asset
# name for an ARM machine would silently install the wrong architecture
# (confirmed live: an ARM Linux machine matched against linux-cpu/
# linux-cuda12.4/linux-rocm7.2, all of which are x86_64-only builds). So
# Linux/Windows require a recognized x86_64 identifier; anything else - and
# any OS other than Darwin/Linux/Windows - is unsupported_platform instead
# of a guessed match.
_TEXTGENWEBUI_X86_64_MACHINES = {"x86_64", "amd64"}


def _textgenwebui_is_x86_64(machine: str) -> bool:
    return machine.lower() in _TEXTGENWEBUI_X86_64_MACHINES


def _textgenwebui_platform_supported(system: str, machine: str) -> bool:
    """Whether a real release asset exists for this OS/arch combo. Split out
    from _textgenwebui_asset_name (which also needs HardwareInfo to pick a
    GPU variant) so has_automated_installer can reuse the exact same
    OS/arch gate while staying cheap and side-effect-free - no
    scan_hardware() call needed just to answer "is this supported at
    all"."""
    if system == "Darwin":
        return True
    if system in ("Windows", "Linux"):
        return _textgenwebui_is_x86_64(machine)
    return False


def _textgenwebui_variant(hw: HardwareInfo) -> str:
    """Best-effort GPU-variant choice from already-collected hardware info.
    A wrong guess just means slower (or CPU-mode) inference, never a
    broken install, so this favors safe/broad compatibility over squeezing
    out maximum performance: cuda12.4 over the newer cuda13.1 (needs a
    newer driver), vulkan over guessing at ROCm on Windows (research found
    ROCm offered as Linux-only in the app's own GPU picker)."""
    gpu_name = (hw.gpu_name or "").lower()
    system = platform.system()
    if "nvidia" in gpu_name:
        return "cuda12.4"
    if "amd" in gpu_name or "radeon" in gpu_name:
        return "rocm7.2" if system == "Linux" else "vulkan"
    if gpu_name:
        return "vulkan"
    return "cpu"


def _textgenwebui_asset_name(hw: HardwareInfo) -> str | None:
    system = platform.system()
    machine = platform.machine()
    if not _textgenwebui_platform_supported(system, machine):
        return None
    if system == "Darwin":
        arch = "arm64" if machine == "arm64" else "x86_64"
        return f"macos-{arch}"
    if system == "Windows":
        return f"windows-{_textgenwebui_variant(hw)}"
    return f"linux-{_textgenwebui_variant(hw)}"


_WINDOWS_RESERVED_ARCHIVE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _safe_archive_member_parts(name: str) -> tuple[str, ...]:
    """Return a platform-independent safe relative archive path.

    ZIP and tar member names use forward slashes. Reject alternate Windows
    spellings too so an archive has the same meaning on every supported
    Python/OS combination instead of becoming unsafe only after it moves to
    Windows.
    """
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise OSError(f"unsafe archive member path: {name!r}")

    raw_parts = name.split("/")
    if any(part == ".." for part in raw_parts):
        raise OSError(f"unsafe archive member path: {name!r}")
    parts = tuple(part for part in raw_parts if part not in ("", "."))
    if not parts:
        raise OSError(f"unsafe archive member path: {name!r}")

    for part in parts:
        # Colons include drive-relative/absolute paths and NTFS alternate
        # data streams. Trailing spaces/dots and DOS device names can alias
        # other paths or devices on Windows even though they are ordinary
        # filename characters on POSIX.
        if ":" in part or part.endswith((" ", ".")):
            raise OSError(f"unsafe archive member path: {name!r}")
        windows_basename = part.split(".", 1)[0].upper()
        if windows_basename in _WINDOWS_RESERVED_ARCHIVE_NAMES:
            raise OSError(f"unsafe archive member path: {name!r}")
    return parts


def _safe_archive_link_target_parts(
    member_parts: tuple[str, ...], linkname: str
) -> tuple[str, ...]:
    """Resolve a tar symlink target without allowing it outside the root.

    Portable TextGen releases contain many ordinary relative symlinks (the
    embedded Python runtime alone has ``python3 -> python3.13``).  Rejecting
    every link makes the official archive unusable, while handing links to
    ``tarfile.extractall`` reintroduces traversal.  Resolve the target
    lexically, require it to stay below the archive's single top-level
    directory, and materialize it only after all regular payloads are done.
    """
    if (
        not linkname
        or "\x00" in linkname
        or "\\" in linkname
        or linkname.startswith("/")
    ):
        raise OSError(f"unsafe archive link target: {linkname!r}")

    resolved = list(member_parts[:-1])
    for part in linkname.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            # Keep the first component: it is the archive's validated root.
            if len(resolved) <= 1:
                raise OSError(f"unsafe archive link target: {linkname!r}")
            resolved.pop()
            continue
        try:
            safe_part = _safe_archive_member_parts(part)
        except OSError as exc:
            raise OSError(f"unsafe archive link target: {linkname!r}") from exc
        if len(safe_part) != 1:
            raise OSError(f"unsafe archive link target: {linkname!r}")
        resolved.append(part)

    if not resolved or resolved[0] != member_parts[0]:
        raise OSError(f"unsafe archive link target: {linkname!r}")
    return tuple(resolved)


def _reject_archive_symlink_descendants(
    entries: Sequence[tuple[object, tuple[str, ...], bool | None, int]],
) -> None:
    """No payload path may use an archive symlink as a parent directory."""
    seen: set[tuple[str, ...]] = set()
    symlinks = {parts for _, parts, is_dir, _ in entries if is_dir is None}
    for _, parts, _, _ in entries:
        if parts in seen:
            raise OSError(f"duplicate archive member path: {'/'.join(parts)!r}")
        seen.add(parts)
        if any(parts[:depth] in symlinks for depth in range(1, len(parts))):
            raise OSError(f"archive member traverses a symlink: {'/'.join(parts)!r}")


def _archive_top_level(
    entries: Sequence[tuple[object, tuple[str, ...], bool | None, int]],
) -> str:
    roots = {parts[0] for _, parts, _, _ in entries}
    if len(roots) != 1:
        raise OSError("archive must contain exactly one top-level directory")
    return next(iter(roots))


def _validated_zip_entries(
    zf: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool, int]]:
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...], bool, int]] = []
    for member in zf.infolist():
        parts = _safe_archive_member_parts(member.filename)
        is_dir = member.is_dir()
        unix_mode = member.external_attr >> 16 if member.create_system == 3 else 0
        file_type = stat_module.S_IFMT(unix_mode)
        allowed_type = stat_module.S_IFDIR if is_dir else stat_module.S_IFREG
        if file_type not in (0, allowed_type):
            raise OSError(f"unsupported archive member type: {member.filename!r}")
        mode = unix_mode & 0o777
        if mode == 0:
            mode = 0o755 if is_dir else 0o644
        entries.append((member, parts, is_dir, mode))
    return entries


def _validated_tar_entries(
    tf: tarfile.TarFile,
) -> list[tuple[tarfile.TarInfo, tuple[str, ...], bool | None, int]]:
    entries: list[tuple[tarfile.TarInfo, tuple[str, ...], bool | None, int]] = []
    for member in tf.getmembers():
        parts = _safe_archive_member_parts(member.name)
        if member.isdir():
            is_dir = True
        elif member.isfile():
            is_dir = False
        elif member.issym():
            _safe_archive_link_target_parts(parts, member.linkname)
            # None distinguishes a validated symlink from files/directories.
            is_dir = None
        else:
            # Hard links, devices, FIFOs, and other special archive entries
            # remain unsupported. This is explicit rather than relying on
            # Python-version-dependent tar extraction filter defaults.
            raise OSError(f"unsupported archive member type: {member.name!r}")
        entries.append((member, parts, is_dir, member.mode & 0o777))
    _reject_archive_symlink_descendants(entries)
    return entries


def _prepare_archive_output(
    staging: Path, parts: tuple[str, ...], is_dir: bool
) -> Path:
    output = staging.joinpath(*parts)
    if is_dir:
        output.mkdir(parents=True, exist_ok=True)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _extract_textgenwebui_archive(archive_path: Path, dest_dir: Path) -> Path:
    """Extracts the portable release into dest_dir and returns the
    resulting top-level folder (named textgen-<version> by the archive
    itself - verified against real release bytes).

    All entries are validated before writing anything, then ordinary files
    and directories are copied manually into a fresh staging directory.
    This gives Python 3.10 through 3.14 the same policy and avoids both the
    historical unfiltered tar behavior and destination symlink traversal.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".omm-extract-", dir=dest_dir) as temp_name:
        staging = Path(temp_name)
        directory_modes: list[tuple[Path, int]] = []
        pending_symlinks: list[tuple[Path, str]] = []

        if archive_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive_path) as zf:
                entries = _validated_zip_entries(zf)
                top_level = _archive_top_level(entries)
                final_path = dest_dir / top_level
                if final_path.exists() or final_path.is_symlink():
                    raise OSError(f"archive destination already exists: {final_path}")
                for member, parts, is_dir, mode in entries:
                    output = _prepare_archive_output(staging, parts, is_dir is True)
                    if is_dir:
                        directory_modes.append((output, mode))
                        continue
                    with zf.open(member, "r") as source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
                    output.chmod(mode)
        else:
            with tarfile.open(archive_path) as tf:
                entries = _validated_tar_entries(tf)
                top_level = _archive_top_level(entries)
                final_path = dest_dir / top_level
                if final_path.exists() or final_path.is_symlink():
                    raise OSError(f"archive destination already exists: {final_path}")
                for member, parts, is_dir, mode in entries:
                    output = _prepare_archive_output(staging, parts, is_dir is True)
                    if is_dir is True:
                        directory_modes.append((output, mode))
                        continue
                    if is_dir is None:
                        pending_symlinks.append((output, member.linkname))
                        continue
                    source = tf.extractfile(member)
                    if source is None:
                        raise OSError(f"could not read archive member: {member.name!r}")
                    with source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
                    output.chmod(mode)

        # Links are deliberately last: no subsequent archive write can follow
        # one into another location. Their targets were already proven to stay
        # under this archive root by _safe_archive_link_target_parts.
        for output, linkname in pending_symlinks:
            # Archive link names always use POSIX separators.  Build a native
            # relative Path so Windows stores a usable reparse target too;
            # passing ``../lib/foo`` through unchanged creates a link that
            # pathlib can identify but cannot follow on Windows.
            output.symlink_to(Path(*linkname.split("/")))

        # Keep parent directories writable while files and links are being
        # created; apply archive permissions only after all payloads are in place.
        for directory, mode in sorted(
            directory_modes, key=lambda item: len(item[0].parts), reverse=True
        ):
            directory.chmod(mode)

        staged_root = staging / top_level
        if staged_root.is_symlink() or not staged_root.is_dir():
            raise OSError("archive top-level entry must be a directory")
        if final_path.exists() or final_path.is_symlink():
            raise OSError(f"archive destination already exists: {final_path}")
        staged_root.rename(final_path)
        return final_path


def _install_textgenwebui(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    return EngineInstallResult(
        "textgenwebui", "unsupported_platform",
        f"Automatic installation is disabled because release artifacts have no pinned SHA-256. Install manually from {_TEXTGENWEBUI_RELEASES_URL}",
    )


def _engine_storage_dir(key: str) -> Path | None:
    if key == "ollama":
        return ollama_models_dir()
    if key == "lmstudio":
        return lmstudio_models_dir()
    if key == "anythingllm":
        return anythingllm_ollama_models_dir()
    if key == "mstystudio":
        return mstystudio_models_dir()
    if key == "textgenwebui":
        return textgenwebui_models_dir()
    if key == "koboldcpp":
        return koboldcpp_models_dir()
    # Jan stores only a tiny YAML file containing the central absolute path.
    return None


def ollama_native_copy_may_be_required() -> bool:
    """Whether the current Ollama version could require ``ollama create``.

    An unknown version is budgeted pessimistically: the compatibility probe
    happens only after the GGUF exists, and a rejection makes Ollama write a
    full native blob copy.
    """
    # Treat this as a worst-case capacity question, not a prediction. The
    # Ollama binary/daemon can be upgraded between preflight and link, so a
    # cached compatible version is not strong enough evidence to promise a
    # zero-copy install during an unattended run.
    return True


def disk_copy_risks(source_path: Path, *, only_engine: str | None = None) -> list[DiskCopyRisk]:
    """Return full-model copies that must be included in install preflight.

    POSIX engines use symlinks. Windows destinations on another volume may
    need a real copy when Developer Mode is unavailable. System Ollama is a
    special case on every OS because its native compatibility fallback
    imports a second full blob even when source and model store share a
    volume.

    `only_engine` restricts the check to a single engine key (e.g.
    `omm contribute` only needs whichever engine it is benchmarking against
    this session); `None` checks every installed engine, as before.
    """
    risks: list[DiskCopyRisk] = []
    source_volume = storage_volume_key(source_path)
    for spec in ENGINES:
        if only_engine is not None and spec.key != only_engine:
            continue
        if not is_engine_installed(spec.key):
            continue
        target = _engine_storage_dir(spec.key)
        if target is None:
            continue
        cross_volume_windows = (
            platform.system() == "Windows" and storage_volume_key(target) != source_volume
        )
        native_ollama = spec.key == "ollama" and ollama_native_copy_may_be_required()
        if cross_volume_windows or native_ollama:
            reason = (
                "Ollama may need a native full-model import"
                if native_ollama
                else f"{spec.label} is on another Windows volume"
            )
            risks.append(DiskCopyRisk(target, spec.label, reason))
    return risks


def link_engine(
    key: str,
    gguf_path: Path,
    *,
    repo_id: str | None,
    ollama_tag: str,
    force: bool = False,
) -> str | None:
    """Link `gguf_path` into the named engine (must already be confirmed
    installed via is_engine_installed). Returns an optional warning message
    to surface to the user; raises LinkError on failure.

    `force` reclaims a destination not recognized as omm's own (see
    `link_file`/`link_ollama`) instead of raising a conflict LinkError."""
    messages: list[str] = []

    def report_copy(_source: Path, destination: Path, size_bytes: int) -> None:
        messages.append(
            f"Windows could not create a zero-copy link, so omm copied "
            f"{size_bytes / 1024**3:.1f} GiB to {destination}. This uses additional disk space."
        )

    if key == "ollama":
        has_chat_template = link_ollama(gguf_path, ollama_tag, on_copy=report_copy, force=force)
        if not has_chat_template:
            messages.append(
                "This GGUF has no embedded chat template - Ollama will fall "
                "back to raw completion (no chat formatting)."
            )
    elif key == "lmstudio":
        link_lmstudio(gguf_path, repo_id, on_copy=report_copy, force=force)
    elif key == "jan":
        link_jan(gguf_path, ollama_tag)
    elif key == "anythingllm":
        link_ollama(
            gguf_path,
            ollama_tag,
            models_dir=anythingllm_ollama_models_dir(),
            verify_compat=False,
            on_copy=report_copy,
            force=force,
        )
    elif key == "mstystudio":
        link_custom_directory(gguf_path, mstystudio_models_dir(), on_copy=report_copy, force=force)
    elif key == "textgenwebui":
        models_dir = textgenwebui_models_dir()
        if models_dir is None:
            raise LinkError("text-generation-webui not found.")
        link_custom_directory(gguf_path, models_dir, on_copy=report_copy, force=force)
    elif key == "koboldcpp":
        models_dir = koboldcpp_models_dir()
        if models_dir is None:
            raise LinkError("KoboldCpp not found.")
        link_custom_directory(gguf_path, models_dir, on_copy=report_copy, force=force)
    else:
        raise ValueError(f"unknown engine: {key}")
    return "\n".join(messages) or None


def unlink_engine(
    key: str,
    filename: str,
    entry: dict,
    *,
    defer_ollama_unlink: Callable[[Path, str, Path | None, str | None], None] | None = None,
) -> None:
    """`defer_ollama_unlink`, if given, is called instead of `unlink_ollama`
    for the "ollama"/"anythingllm" keys - a caller removing many models at
    once (e.g. `omm uninstall all`) passes `_PendingOllamaUnlinks.add` here
    so the expensive orphaned-blob rescan runs once per models_dir at the
    end instead of once per model (see issue #181)."""
    ollama_tag = entry.get("ollama_name") or sanitize_ollama_tag(filename)
    expected_source = MODELS_DIR / filename
    expected_sha256 = entry.get("sha256")
    if not isinstance(expected_sha256, str):
        expected_sha256 = None

    def _unlink_ollama(target_models_dir: Path) -> None:
        if defer_ollama_unlink is not None:
            defer_ollama_unlink(target_models_dir, ollama_tag, expected_source, expected_sha256)
        else:
            unlink_ollama(
                ollama_tag,
                models_dir=target_models_dir,
                expected_source=expected_source,
                expected_content_sha256=expected_sha256,
            )

    if key == "ollama":
        _unlink_ollama(ollama_models_dir())
    elif key == "lmstudio":
        unlink_lmstudio(filename, entry.get("repo_id"))
    elif key == "jan":
        unlink_jan(ollama_tag, expected_source=expected_source)
    elif key == "anythingllm":
        _unlink_ollama(anythingllm_ollama_models_dir())
    elif key == "mstystudio":
        unlink_custom_directory(filename, mstystudio_models_dir())
    elif key == "textgenwebui":
        models_dir = textgenwebui_models_dir()
        if models_dir is not None:
            unlink_custom_directory(filename, models_dir)
    elif key == "koboldcpp":
        models_dir = koboldcpp_models_dir()
        if models_dir is not None:
            unlink_custom_directory(filename, models_dir)


def autoremove_engine(key: str) -> int:
    if key == "ollama":
        blobs_removed, _manifests_removed = autoremove_ollama()
        return blobs_removed
    if key == "lmstudio":
        return autoremove_lmstudio()
    if key == "jan":
        return autoremove_jan()
    if key == "anythingllm":
        blobs_removed, _manifests_removed = autoremove_ollama(models_dir=anythingllm_ollama_models_dir())
        return blobs_removed
    if key == "mstystudio":
        return autoremove_custom_directory(mstystudio_models_dir())
    if key == "textgenwebui":
        models_dir = textgenwebui_models_dir()
        return autoremove_custom_directory(models_dir) if models_dir is not None else 0
    if key == "koboldcpp":
        models_dir = koboldcpp_models_dir()
        return autoremove_custom_directory(models_dir) if models_dir is not None else 0
    return 0


# --- LM Studio daemon-lifecycle public API --------------------------------


def lmstudio_daemon_reachable() -> bool:
    """True iff `lms server status` reports running. Mirrors
    benchmark.ollama_daemon_reachable()'s role for the LM Studio path."""
    lms_path = _lms_cli_path()
    if lms_path is None:
        return False
    status = _lmstudio_server_status(lms_path)
    return status is not None and status.get("running") is True


def lmstudio_server_port() -> int | None:
    """Live port from `lms server status --json`, or None if not running.
    Never assume the default 1234 - the port is user-configurable."""
    lms_path = _lms_cli_path()
    if lms_path is None:
        return None
    status = _lmstudio_server_status(lms_path)
    if status is None or not status.get("running"):
        return None
    return status.get("port")


def start_lmstudio_daemon(timeout: float = 30.0) -> bool:
    """Best-effort `lms server start`; True iff running after the call
    (whether it was already running or freshly started)."""
    lms_path = _lms_cli_path()
    if lms_path is None:
        return False
    return _start_lmstudio_server(lms_path, timeout=timeout)


def stop_lmstudio_daemon() -> None:
    """Best-effort `lms server stop`. Caller's responsibility to only call
    this when omm itself started the daemon (mirrors
    benchmark.stop_ollama_daemon's contract, but LM Studio's lifecycle is a
    named background service, not a Popen omm owns directly - no handle to
    pass back)."""
    lms_path = _lms_cli_path()
    if lms_path is not None:
        _stop_lmstudio_server(lms_path)


def resolve_lmstudio_model(repo_id: str | None, filename: str) -> dict | None:
    """Resolve a linked model to its LM Studio ls entry (modelKey +
    metadata), by the same path-matching _lmstudio_model_key already uses.
    Returns None if `lms` is missing, the server is down, or no match is
    found. Return shape:
      {"model_key": str, "architecture": str | None,
       "quantization_name": str | None, "quantization_bits": int | None,
       "params_string": str | None, "max_context_length": int | None,
       "trained_for_tool_use": bool}
    """
    lms_path = _lms_cli_path()
    if lms_path is None:
        return None

    publisher, repo = _lmstudio_publisher_repo(repo_id, filename)
    models = _lmstudio_list_models(lms_path)
    if models is None:
        return None

    # Use existing path-matching logic to get the model key
    model_key = _lmstudio_find_model_key(models, publisher, repo, filename)
    if model_key is None:
        return None

    # Second pass: find the entry by modelKey and extract metadata
    for entry in models:
        if not isinstance(entry, dict):
            continue

        # Skip non-llm types (e.g., embedding models like mmproj)
        if entry.get("type") != "llm":
            continue

        # Match by modelKey (which we already know is correct via path-matching)
        if entry.get("modelKey") == model_key:
            # Extract quantization metadata from nested object
            quant = entry.get("quantization")
            if not isinstance(quant, dict):
                quant = {}

            # Extract metadata from the raw entry
            return {
                "model_key": model_key,
                "architecture": entry.get("architecture"),
                "quantization_name": quant.get("name"),
                "quantization_bits": quant.get("bits"),
                "params_string": entry.get("paramsString"),
                "max_context_length": entry.get("maxContextLength"),
                "trained_for_tool_use": entry.get("trainedForToolUse", False),
            }

    return None


def unload_lmstudio_model(model_key: str) -> bool:
    """Best-effort `lms unload`. Wraps _lms_unload, returns whether the
    subprocess ran without raising (matches _lms_unload's soft-fail
    contract - always True unless the CLI itself is missing)."""
    lms_path = _lms_cli_path()
    if lms_path is None:
        return False
    result = _lms_unload(lms_path, model_key)
    # Preserve compatibility with older/mocked best-effort unload helpers
    # that returned None on success while allowing the real helper's new
    # False result to reach the memory guard.
    return result is not False
