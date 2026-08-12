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
user must opt into explicitly) does a real directory scan, confirmed by
reading koboldcpp's source and by running it live with --admindir pointed
at a symlinked test model and querying its admin API. omm can't detect a
default install path, so it heuristically looks for the binary in common
locations and links into a `models` folder next to it; the user still needs
to launch koboldcpp with --admindir pointed at that folder themselves.

text-generation-webui likewise has no fixed OS install location (a git
clone anywhere), so omm heuristically looks for its directory by checking
for the project's own marker files (server.py, one_click.py).
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import platform
import re
import shutil
import time
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from omm import config
from omm.gguf import read_gguf_metadata
from omm.hashutil import sha256_file
from omm.atomic import atomic_write_text, backup_corrupt_file, locked
from omm.config import LINK_OWNERSHIP_PATH


def lmstudio_home_dir() -> Path:
    """LM Studio's data dir. Newer versions default to ~/.lmstudio, but keep
    using ~/.cache/lm-studio (the old default) if that's what's already
    there - LM Studio itself does this via a ~/.lmstudio-home-pointer file
    when it finds a pre-existing legacy install. Confirmed on a real 0.4.19
    Homebrew install where the pointer redirected to ~/.cache/lm-studio.
    """
    pointer = Path.home() / ".lmstudio-home-pointer"
    if pointer.exists():
        return Path(pointer.read_text().strip())
    if (Path.home() / ".cache" / "lm-studio").exists():
        return Path.home() / ".cache" / "lm-studio"
    return Path.home() / ".lmstudio"


def lmstudio_models_dir() -> Path:
    override = os.environ.get("OMM_LMSTUDIO_MODELS_DIR")
    if override:
        return Path(override).expanduser()
    return lmstudio_home_dir() / "models"


def ollama_models_dir() -> Path:
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


def is_lmstudio_installed() -> bool:
    # A headless llmster install (the `lms` CLI + daemon, no GUI) is a
    # real, usable install with no app bundle at all - check it first.
    if _lms_cli_path() is not None:
        return True
    if platform.system() == "Darwin":
        return _app_bundle_installed("LM Studio")
    return lmstudio_home_dir().exists()


def is_ollama_installed() -> bool:
    # Homebrew's ollama-app cask installs Ollama.app; the plain `ollama`
    # formula (common on Linux/Homebrew-CLI setups) installs only the
    # `ollama` binary with no bundle - check both so a CLI-only install
    # isn't reported as "not installed".
    if platform.system() == "Darwin":
        return _app_bundle_installed("Ollama") or shutil.which("ollama") is not None
    return (Path.home() / ".ollama").exists() or shutil.which("ollama") is not None


class LinkError(Exception):
    pass


def _link_key(path: Path) -> str:
    """Stable key without resolving a link target that may no longer exist."""
    return str(path.expanduser().absolute())


def _load_link_ownership() -> dict[str, dict[str, object]]:
    if not LINK_OWNERSHIP_PATH.exists():
        return {}
    try:
        data = json.loads(LINK_OWNERSHIP_PATH.read_text())
    except (OSError, json.JSONDecodeError):
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
    _update_link_ownership(
        dst,
        record,
    )


def _record_hardlink(dst: Path, src: Path) -> None:
    _record_ownership(dst, src, "hardlink")


def _record_symlink(dst: Path, src: Path) -> None:
    _record_ownership(dst, src, "symlink")


def _owned_hardlink(path: Path) -> bool:
    record = _load_link_ownership().get(_link_key(path))
    if not record or record.get("kind") != "hardlink" or path.is_symlink() or not path.exists():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return record.get("device") == stat.st_dev and record.get("inode") == stat.st_ino


def _owned_symlink(path: Path) -> bool:
    record = _load_link_ownership().get(_link_key(path))
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


def _owned_copy(path: Path) -> bool:
    record = _load_link_ownership().get(_link_key(path))
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


def _owned_manifest(path: Path) -> bool:
    record = _load_link_ownership().get(_link_key(path))
    if not record or record.get("kind") != "manifest" or not path.exists() or path.is_symlink():
        return False
    stat = path.stat()
    return record.get("device") == stat.st_dev and record.get("inode") == stat.st_ino


def unlink_owned_link(path: Path) -> bool:
    """Remove an omm symlink or a recorded, unchanged omm hard link.

    Never removes an unrecorded regular file.  Returns whether a link was
    removed so callers can preserve ordinary user files at managed paths.
    """
    if _owned_symlink(path):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    if _owned_hardlink(path):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    if _owned_copy(path):
        path.unlink()
        _update_link_ownership(path, None)
        return True
    return False


def _unlink_owned_link_with_retry(path: Path, attempts: int = 8) -> bool:
    for attempt in range(attempts):
        try:
            return unlink_owned_link(path)
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


def link_file(src: Path, dst: Path, *, on_copy: CopyReporter | None = None) -> str:
    """Expose ``src`` at ``dst`` and return symlink/hardlink/copy.

    Windows file junctions are not applicable here (they only target
    directories). A hard link is attempted first because it needs no
    Developer Mode; a symlink covers cross-volume destinations when
    permitted, and an ownership-recorded copy is the last-resort fallback.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LinkError(f"Could not create directory {dst.parent}: {e}") from e
    if dst.exists() or dst.is_symlink():
        if not unlink_owned_link(dst):
            # Pre-ownership-registry omm links have no metadata. During an
            # explicit relink, adopt only a destination proved to be this
            # exact source; never infer ownership from a matching filename.
            if not _matches_requested_link(src, dst):
                raise LinkError(f"Refusing to replace unowned existing file at {dst}.")
            if dst.is_symlink():
                _record_symlink(dst, src)
            else:
                _record_hardlink(dst, src)
            return "symlink" if dst.is_symlink() else "hardlink"

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
    if repo_id and "/" in repo_id:
        publisher, repo = repo_id.split("/", 1)
        return publisher, repo
    return "local", Path(filename).stem


def link_lmstudio(
    gguf_path: Path, repo_id: str | None, *, on_copy: CopyReporter | None = None
) -> Path:
    publisher, repo = _lmstudio_publisher_repo(repo_id, gguf_path.name)
    dst = lmstudio_models_dir() / publisher / repo / gguf_path.name
    link_file(gguf_path, dst, on_copy=on_copy)
    return dst


def link_custom_directory(
    gguf_path: Path, directory: Path, *, on_copy: CopyReporter | None = None
) -> Path:
    """Expose a central GGUF in an arbitrary local application's model directory."""
    destination = directory.expanduser() / gguf_path.name
    link_file(gguf_path, destination, on_copy=on_copy)
    return destination


def unlink_custom_directory(filename: str, directory: Path) -> None:
    dst = directory.expanduser() / filename
    unlink_owned_link(dst)


def autoremove_custom_directory(directory: Path) -> int:
    """Delete broken symlinks omm placed directly in `directory` (flat, not
    recursive - link_custom_directory never nests, so nothing else is
    omm's to clean up here). Returns the number removed."""
    directory = directory.expanduser()
    if not directory.exists():
        return 0
    removed = 0
    for path in directory.iterdir():
        if path.is_symlink() and not path.exists():
            if unlink_owned_link(path):
                removed += 1
    return removed


def unlink_lmstudio(filename: str, repo_id: str | None) -> None:
    publisher, repo = _lmstudio_publisher_repo(repo_id, filename)
    dst = lmstudio_models_dir() / publisher / repo / filename
    if unlink_owned_link(dst):
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
    installed, so also check the well-known bootstrap location directly -
    confirmed via `lms bootstrap` against a real LM Studio 0.4.20 install,
    which installs to <lmstudio_home_dir>/bin/lms."""
    found = shutil.which("lms")
    if found is not None:
        return found
    candidate = lmstudio_home_dir() / "bin" / "lms"
    return str(candidate) if candidate.is_file() else None


def _lmstudio_server_status(lms_path: str, timeout: float = 5) -> dict | None:
    """Ask `lms` whether its local server is running and on what port -
    the port is user-configurable, so this is the only reliable source for
    it. None on any failure to ask (never guess a default port)."""
    try:
        result = subprocess.run(
            [lms_path, "server", "status", "--json"],
            capture_output=True, text=True, timeout=timeout,
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
        or not isinstance(data.get("port"), int)
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
            [lms_path, "server", "start"], capture_output=True, text=True, timeout=timeout
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
        subprocess.run([lms_path, "server", "stop"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _lmstudio_list_models(lms_path: str, timeout: float = 15) -> list[dict] | None:
    """List models `lms` currently sees on disk. None on any failure."""
    try:
        result = subprocess.run(
            [lms_path, "ls", "--json"], capture_output=True, text=True, timeout=timeout,
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
    expected = f"{publisher}/{repo}/{filename}"
    for entry in models:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.replace("\\", "/") == expected:
            key = entry.get("modelKey")
            return key if isinstance(key, str) else None
    return None


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


def _lms_unload(lms_path: str, model_key: str) -> None:
    """Best-effort isolation cleanup after a probe, mirroring
    quality.unload_model's role for Ollama. Confirmed against a real LM
    Studio instance that unloading a not-currently-loaded identifier exits
    cleanly rather than raising, but this still never propagates a
    failure either way."""
    try:
        subprocess.run([lms_path, "unload", model_key], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


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
    return re.sub(r"[^a-z0-9._-]+", "-", name).strip("-")


def _guess_param_size(filename: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)[Bb](?:[-_.]|$)", filename)
    return f"{m.group(1)}B" if m else "unknown"


def _guess_quant(filename: str) -> str:
    m = re.search(r"(Q\d(?:_[A-Z0-9]+)*)", filename, re.IGNORECASE)
    return m.group(1).upper() if m else "unknown"


def link_ollama(
    gguf_path: Path,
    model_name: str,
    models_dir: Path | None = None,
    verify_compat: bool = True,
    *,
    on_copy: CopyReporter | None = None,
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

    try:
        gguf_meta = read_gguf_metadata(gguf_path, {"general.architecture", "tokenizer.chat_template"})
    except (struct.error, KeyError) as e:
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
        _fallback_to_native_create(gguf_path, model_name, models_dir)
        return True

    model_sha256 = sha256_file(gguf_path)
    model_digest = f"sha256:{model_sha256}"

    blobs_dir = models_dir / "blobs"
    try:
        blobs_dir.mkdir(parents=True, exist_ok=True)

        model_blob = blobs_dir / f"sha256-{model_sha256}"
        # A matching content-addressed blob may be owned by Ollama or another
        # manifest. It is already usable; never replace it.
        if not model_blob.exists() and not model_blob.is_symlink():
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
        if not config_blob.exists() and not config_blob.is_symlink():
            config_blob.write_bytes(config_bytes)
            try:
                _record_ownership(config_blob, None, "copy")
            except Exception:
                config_blob.unlink(missing_ok=True)
                raise

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

        manifest_dir = (
            models_dir / "manifests" / "registry.ollama.ai" / "library" / model_name
        )
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "latest"
        if manifest_path.exists() or manifest_path.is_symlink():
            if not _owned_manifest(manifest_path):
                raise LinkError(f"Refusing to replace unowned Ollama manifest at {manifest_path}.")
            manifest_path.unlink()
            _update_link_ownership(manifest_path, None)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        try:
            _record_ownership(manifest_path, None, "manifest")
        except OSError:
            manifest_path.unlink(missing_ok=True)
            raise
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
    exe = shutil.which("ollama")
    if exe is None:
        return None
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
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
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if cache.get("ollama_version") != ollama_version:
        return None
    compatible = cache.get("compatible")
    return compatible if isinstance(compatible, bool) else None


def _record_manifest_format_result(ollama_version: str, compatible: bool) -> None:
    try:
        path = _ollama_manifest_compat_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ollama_version": ollama_version, "compatible": compatible}))
    except OSError:
        pass


def _ollama_accepts_manifest(model_name: str) -> bool | None:
    """Ask the real `ollama` CLI to read back a manifest omm just wrote.
    True if Ollama parses it fine, False if it rejects/can't find it (our
    hand-rolled shape has drifted from what this Ollama version expects),
    None if the daemon isn't reachable at all - nothing to compare against,
    not a format problem."""
    exe = shutil.which("ollama")
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "show", model_name], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if "could not connect" in result.stderr.lower():
        return None
    return False


def _fallback_to_native_create(gguf_path: Path, model_name: str, models_dir: Path) -> None:
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
    exe = shutil.which("ollama")
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
        unlink_ollama(model_name, models_dir=models_dir)

    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    before_blobs = {path.name for path in blobs_dir.iterdir() if path.is_file()}

    def transaction_blobs() -> list[Path]:
        try:
            return [
                path
                for path in blobs_dir.iterdir()
                if path.is_file() and path.name not in before_blobs
            ]
        except OSError:
            return []

    def cleanup_transaction() -> None:
        try:
            manifest_path.unlink(missing_ok=True)
            _update_link_ownership(manifest_path, None)
        except OSError:
            pass
        for blob in transaction_blobs():
            try:
                blob.unlink(missing_ok=True)
            except OSError:
                pass

    with tempfile.TemporaryDirectory() as tmp:
        modelfile = Path(tmp) / "Modelfile"
        modelfile.write_text(f"FROM {gguf_path}\n")
        try:
            result = subprocess.run(
                [exe, "create", model_name, "-f", str(modelfile)],
                capture_output=True,
                text=True,
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
    unlink_ollama(model_name, models_dir=models_dir)
    _fallback_to_native_create(gguf_path, model_name, models_dir)
    return True


def _manifest_blob_digests(manifest: dict) -> set[str]:
    digests = {
        layer.get("digest", "").replace(":", "-")
        for layer in manifest.get("layers", [])
        if layer.get("digest")
    }
    config_digest = manifest.get("config", {}).get("digest")
    if config_digest:
        digests.add(config_digest.replace(":", "-"))
    return digests


def unlink_ollama(model_name: str, models_dir: Path | None = None) -> None:
    if models_dir is None:
        models_dir = ollama_models_dir()
    manifest_path = (
        models_dir
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / model_name
        / "latest"
    )
    if not manifest_path.exists():
        return
    if not _owned_manifest(manifest_path):
        return
    try:
        manifest = json.loads(manifest_path.read_text())
        model_digests = _manifest_blob_digests(manifest)
    except (OSError, json.JSONDecodeError):
        model_digests = set()
    # Blobs are content-addressed and can be shared by a user manifest or
    # another omm model. Only remove an omm-owned blob after no remaining
    # manifest references its content digest.
    try:
        manifest_path.unlink()
    except OSError:
        return
    _update_link_ownership(manifest_path, None)
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass

    # An omm-owned link/copy can be reclaimed once no remaining manifest
    # references its content digest. This matters on Windows: deleting the
    # hub filename alone does not free an NTFS hard link, and a loaded model
    # may retain a handle until Ollama confirms it has unloaded.
    manifests_root = models_dir / "manifests"
    referenced = set()
    if manifests_root.exists():
        for other in manifests_root.rglob("*"):
            if not other.is_file():
                continue
            try:
                data = json.loads(other.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            referenced.update(_manifest_blob_digests(data))
    for digest in model_digests - referenced:
        _unlink_owned_link_with_retry(models_dir / "blobs" / digest)


# --- Autoremove (broken symlink cleanup) ------------------------------


def autoremove_lmstudio() -> int:
    """Delete broken LM Studio symlinks (source .gguf no longer exists).
    Returns the number removed."""
    base = lmstudio_models_dir()
    if not base.exists():
        return 0

    removed = 0
    for path in list(base.rglob("*")):
        if path.is_symlink() and not path.exists():
            if unlink_owned_link(path):
                removed += 1
                for parent in (path.parent, path.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
    return removed


def autoremove_ollama(models_dir: Path | None = None) -> tuple[int, int]:
    """Delete broken Ollama model-layer blob symlinks and any manifests
    that reference them. Returns (blobs_removed, manifests_removed)."""
    if models_dir is None:
        models_dir = ollama_models_dir()
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    if not blobs_dir.exists():
        return (0, 0)

    broken_digests = set()
    for blob in blobs_dir.iterdir():
        if blob.is_symlink() and not blob.exists():
            if unlink_owned_link(blob):
                broken_digests.add(blob.name)

    manifests_removed = 0
    if broken_digests and manifests_root.exists():
        for manifest_path in list(manifests_root.rglob("latest")):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            layer_digests = {
                layer["digest"].replace(":", "-") for layer in manifest.get("layers", [])
            }
            if layer_digests & broken_digests and _owned_manifest(manifest_path):
                try:
                    manifest_path.unlink()
                except OSError:
                    continue
                _update_link_ownership(manifest_path, None)
                manifests_removed += 1
                try:
                    manifest_path.parent.rmdir()
                except OSError:
                    pass

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


def is_anythingllm_installed() -> bool:
    if platform.system() == "Darwin":
        return _app_bundle_installed("AnythingLLM")
    return anythingllm_app_dir().exists()


# --- Jan (llamacpp-extension, model.yml manifest) ---------------------------


def jan_app_dir() -> Path:
    return _app_data_dir("Jan")


def jan_models_dir() -> Path:
    return jan_app_dir() / "data" / "llamacpp" / "models"


def is_jan_installed() -> bool:
    if platform.system() == "Darwin":
        return _app_bundle_installed("Jan")
    return jan_app_dir().exists()


def _jan_model_yaml_path(model_id: str) -> Path:
    return jan_models_dir() / model_id / "model.yml"


def link_jan(gguf_path: Path, model_id: str) -> Path:
    """Register `gguf_path` with Jan by writing a model.yml manifest that
    points model_path straight at it - no symlink needed, since Jan's own
    local-file import does the same (stores the absolute path as-is)."""
    config_path = _jan_model_yaml_path(model_id)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            f'model_path: "{gguf_path}"\n'
            f'name: "{model_id}"\n'
            f"size_bytes: {gguf_path.stat().st_size}\n"
        )
    except OSError as e:
        raise LinkError(f"Could not write Jan manifest at {config_path}: {e}") from e
    return config_path


def unlink_jan(model_id: str) -> None:
    config_path = _jan_model_yaml_path(model_id)
    if config_path.exists():
        try:
            config_path.unlink()
        except OSError:
            return
    try:
        config_path.parent.rmdir()
    except OSError:
        pass


_JAN_MODEL_PATH_RE = re.compile(r'^model_path:\s*"?([^"\n]*)"?\s*$', re.MULTILINE)


def read_jan_model_path(config_path: Path) -> str | None:
    """Pull `model_path` out of a model.yml. Jan's manifest only ever needs
    this one field read back, so a tiny regex stands in for a full YAML
    parser rather than adding a new dependency for it."""
    try:
        text = config_path.read_text()
    except OSError:
        return None
    match = _JAN_MODEL_PATH_RE.search(text)
    return match.group(1) if match else None


def autoremove_jan() -> int:
    """Delete model.yml manifests whose model_path no longer points at an
    existing file. Returns the number removed."""
    models_dir = jan_models_dir()
    if not models_dir.exists():
        return 0
    removed = 0
    for config_path in list(models_dir.glob("*/model.yml")):
        model_path = read_jan_model_path(config_path)
        if model_path and not Path(model_path).exists():
            try:
                config_path.unlink()
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


def is_mstystudio_installed() -> bool:
    if platform.system() == "Darwin":
        return _app_bundle_installed("MstyStudio")
    return mstystudio_app_dir().exists()


# --- KoboldCpp / text-generation-webui (no fixed install location) --------
#
# Neither ships an installer that lands in a standard OS app-data path, so
# omm can't just check a fixed directory the way it does for the apps
# above. Both are heuristically located by looking for a marker
# (the koboldcpp binary itself; text-generation-webui's own source files)
# in a short list of common places a user would keep them.

_HEURISTIC_SEARCH_ROOTS = [
    Path.home(),
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "Applications",
    Path("/Applications"),
]


@lru_cache(maxsize=1)
def find_koboldcpp_binary() -> Path | None:
    for root in _HEURISTIC_SEARCH_ROOTS:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.lower().startswith("koboldcpp"):
                return entry
        for entry in entries:
            if entry.is_dir() and "koboldcpp" in entry.name.lower():
                try:
                    sub_entries = list(entry.iterdir())
                except OSError:
                    continue
                for sub in sub_entries:
                    if sub.is_file() and sub.name.lower().startswith("koboldcpp"):
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


_TEXTGENWEBUI_NAME_HINT = re.compile(r"text-generation-webui|oobabooga", re.IGNORECASE)


@lru_cache(maxsize=1)
def find_textgenwebui_root() -> Path | None:
    for root in _HEURISTIC_SEARCH_ROOTS:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (
                entry.is_dir()
                and _TEXTGENWEBUI_NAME_HINT.search(entry.name)
                and (entry / "server.py").exists()
                and (entry / "one_click.py").exists()
            ):
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
    for this engine key" - onboarding.py calls this instead of keeping its
    own separate set of automated engine keys, so a future engine added to
    one place without the other can't leave the wizard calling
    install_engine() on a key that just raises NotImplementedError.
    Deliberately mirrors install_engine()'s own if/elif shape (add one line
    here per new branch added there)."""
    if key == "ollama":
        return True
    if key == "lmstudio":
        return True
    return False


def install_engine(
    key: str, *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Dispatch table mirroring is_engine_installed()'s if/elif style so
    individual branches stay monkeypatchable in tests. Only "ollama" and
    "lmstudio" have automated installers today (see has_automated_installer);
    the rest raise until a follow-up PR adds them one at a time behind this
    same interface."""
    if key == "ollama":
        return _install_ollama(on_output=on_output)
    if key == "lmstudio":
        return _install_lmstudio(on_output=on_output)
    raise NotImplementedError(f"no automated installer for engine: {key}")


def _stream_subprocess(
    args: list[str], on_output: Callable[[str], None] | None
) -> tuple[int, str] | None:
    """Runs args, streaming each stdout line to on_output as it arrives.
    Returns (returncode, None-marker) via the process wait(), or None if
    the process itself couldn't start (caller turns that into a result)."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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
    system = platform.system()
    returncode: int | None = None
    if system in ("Darwin", "Linux"):
        try:
            returncode = _stream_subprocess(
                ["/bin/sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("ollama", "failed", f"Could not start installer: {e}")
    elif system == "Windows":
        if shutil.which("winget") is None:
            return EngineInstallResult(
                "ollama",
                "unsupported_platform",
                f"winget not found - install Ollama manually from {_OLLAMA_DOWNLOAD_URL}",
            )
        try:
            returncode = _stream_subprocess(
                ["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("ollama", "failed", f"Could not start installer: {e}")
    else:
        return EngineInstallResult(
            "ollama", "unsupported_platform", f"No automated installer for {system}."
        )

    if is_ollama_installed():
        return EngineInstallResult("ollama", "installed", "Ollama installed successfully.")
    detail = f" (installer exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        "ollama",
        "failed",
        f"Installer ran but Ollama still isn't detected{detail}. "
        f"Install manually from {_OLLAMA_DOWNLOAD_URL}",
    )


def _install_lmstudio(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Installs llmster, LM Studio's headless CLI+daemon core - not the
    GUI app. Same official-script pattern as Ollama's installer; see
    is_lmstudio_installed()'s CLI check for why that's still a real
    install."""
    system = platform.system()
    returncode: int | None = None
    if system in ("Darwin", "Linux"):
        try:
            returncode = _stream_subprocess(
                ["/bin/sh", "-c", "curl -fsSL https://lmstudio.ai/install.sh | bash"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("lmstudio", "failed", f"Could not start installer: {e}")
    elif system == "Windows":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return EngineInstallResult(
                "lmstudio",
                "unsupported_platform",
                f"PowerShell not found - install manually from {_LMSTUDIO_DOWNLOAD_URL}",
            )
        try:
            returncode = _stream_subprocess(
                [powershell, "-NoProfile", "-Command", "irm https://lmstudio.ai/install.ps1 | iex"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("lmstudio", "failed", f"Could not start installer: {e}")
    else:
        return EngineInstallResult(
            "lmstudio", "unsupported_platform", f"No automated installer for {system}."
        )

    if is_lmstudio_installed():
        return EngineInstallResult("lmstudio", "installed", "LM Studio installed successfully.")
    detail = f" (installer exited with code {returncode})" if returncode else ""
    return EngineInstallResult(
        "lmstudio",
        "failed",
        f"Installer ran but LM Studio still isn't detected{detail}. "
        f"Install manually from {_LMSTUDIO_DOWNLOAD_URL}",
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


def disk_copy_risks(source_path: Path, *, only_ollama: bool = False) -> list[DiskCopyRisk]:
    """Return full-model copies that must be included in install preflight.

    POSIX engines use symlinks. Windows destinations on another volume may
    need a real copy when Developer Mode is unavailable. System Ollama is a
    special case on every OS because its native compatibility fallback
    imports a second full blob even when source and model store share a
    volume.
    """
    risks: list[DiskCopyRisk] = []
    source_volume = storage_volume_key(source_path)
    for spec in ENGINES:
        if only_ollama and spec.key != "ollama":
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


def link_engine(key: str, gguf_path: Path, *, repo_id: str | None, ollama_tag: str) -> str | None:
    """Link `gguf_path` into the named engine (must already be confirmed
    installed via is_engine_installed). Returns an optional warning message
    to surface to the user; raises LinkError on failure."""
    messages: list[str] = []

    def report_copy(_source: Path, destination: Path, size_bytes: int) -> None:
        messages.append(
            f"Windows could not create a zero-copy link, so omm copied "
            f"{size_bytes / 1024**3:.1f} GiB to {destination}. This uses additional disk space."
        )

    if key == "ollama":
        has_chat_template = link_ollama(gguf_path, ollama_tag, on_copy=report_copy)
        if not has_chat_template:
            messages.append(
                "This GGUF has no embedded chat template - Ollama will fall "
                "back to raw completion (no chat formatting)."
            )
    elif key == "lmstudio":
        link_lmstudio(gguf_path, repo_id, on_copy=report_copy)
    elif key == "jan":
        link_jan(gguf_path, ollama_tag)
    elif key == "anythingllm":
        link_ollama(
            gguf_path,
            ollama_tag,
            models_dir=anythingllm_ollama_models_dir(),
            verify_compat=False,
            on_copy=report_copy,
        )
    elif key == "mstystudio":
        link_custom_directory(gguf_path, mstystudio_models_dir(), on_copy=report_copy)
    elif key == "textgenwebui":
        models_dir = textgenwebui_models_dir()
        if models_dir is None:
            raise LinkError("text-generation-webui not found.")
        link_custom_directory(gguf_path, models_dir, on_copy=report_copy)
    elif key == "koboldcpp":
        models_dir = koboldcpp_models_dir()
        if models_dir is None:
            raise LinkError("KoboldCpp not found.")
        link_custom_directory(gguf_path, models_dir, on_copy=report_copy)
    else:
        raise ValueError(f"unknown engine: {key}")
    return "\n".join(messages) or None


def unlink_engine(key: str, filename: str, entry: dict) -> None:
    ollama_tag = entry.get("ollama_name") or sanitize_ollama_tag(filename)
    if key == "ollama":
        unlink_ollama(ollama_tag)
    elif key == "lmstudio":
        unlink_lmstudio(filename, entry.get("repo_id"))
    elif key == "jan":
        unlink_jan(ollama_tag)
    elif key == "anythingllm":
        unlink_ollama(ollama_tag, models_dir=anythingllm_ollama_models_dir())
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
