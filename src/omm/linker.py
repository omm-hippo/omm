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
import json
import os
import platform
import re
import shutil
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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
    return False


def autoremove_owned_link(path: Path) -> bool:
    """Public custom-directory equivalent of the engine autoremove helpers."""
    if path.is_symlink() and not path.exists():
        return unlink_owned_link(path)
    # Do not automatically delete hard links.  Even a recorded hard link is
    # indistinguishable from a user-owned one after a crash or file-system
    # restore; preserving an orphan is safer than risking user data.
    return False


def link_file(src: Path, dst: Path) -> None:
    """Link `dst` to `src` without duplicating bytes. Prefers a symlink;
    on Windows, where creating a symlink needs Developer Mode or an
    elevated process, falls back to a hard link (no special privilege
    needed on NTFS, and - since it's the same file record - still no
    byte duplication, just limited to same-drive destinations)."""
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
            return
    try:
        dst.symlink_to(src)
        try:
            _record_symlink(dst, src)
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        return
    except OSError as symlink_error:
        if platform.system() != "Windows":
            raise LinkError(
                f"Could not create symlink at {dst}: {symlink_error}."
            ) from symlink_error
    try:
        dst.hardlink_to(src)
        try:
            _record_hardlink(dst, src)
        except Exception:
            # Never leave a hard link we cannot prove omm owns.
            dst.unlink(missing_ok=True)
            raise
    except OSError as hardlink_error:
        raise LinkError(
            f"Could not create symlink or hard link at {dst}: {hardlink_error}. "
            "Enable Developer Mode or run as Administrator, or make sure the "
            "model hub and this destination are on the same drive."
        ) from hardlink_error


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


def link_lmstudio(gguf_path: Path, repo_id: str | None) -> Path:
    publisher, repo = _lmstudio_publisher_repo(repo_id, gguf_path.name)
    dst = lmstudio_models_dir() / publisher / repo / gguf_path.name
    link_file(gguf_path, dst)
    return dst


def link_custom_directory(gguf_path: Path, directory: Path) -> Path:
    """Expose a central GGUF in an arbitrary local application's model directory."""
    destination = directory.expanduser() / gguf_path.name
    link_file(gguf_path, destination)
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


def link_ollama(gguf_path: Path, model_name: str, models_dir: Path | None = None) -> bool:
    """Link into Ollama (or an Ollama-format engine at a different
    models_dir, e.g. AnythingLLM's bundled instance). Returns True if the
    source GGUF has an embedded chat template (Ollama reads it from the
    model blob at runtime), False if none was found and the caller should
    warn the user about it.
    """
    if models_dir is None:
        models_dir = ollama_models_dir()
    model_sha256 = sha256_file(gguf_path)
    model_digest = f"sha256:{model_sha256}"

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

    blobs_dir = models_dir / "blobs"
    try:
        blobs_dir.mkdir(parents=True, exist_ok=True)

        model_blob = blobs_dir / f"sha256-{model_sha256}"
        # A matching content-addressed blob may be owned by Ollama or another
        # manifest. It is already usable; never replace it.
        if not model_blob.exists() and not model_blob.is_symlink():
            link_file(gguf_path, model_blob)

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

    return has_chat_template


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
    # Blobs are content-addressed and can be shared by a user manifest or
    # another omm model. Leave their collection to Ollama; only our manifest
    # is safe to remove here.
    try:
        manifest_path.unlink()
    except OSError:
        return
    _update_link_ownership(manifest_path, None)
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass


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


def link_engine(key: str, gguf_path: Path, *, repo_id: str | None, ollama_tag: str) -> str | None:
    """Link `gguf_path` into the named engine (must already be confirmed
    installed via is_engine_installed). Returns an optional warning message
    to surface to the user; raises LinkError on failure."""
    if key == "ollama":
        has_chat_template = link_ollama(gguf_path, ollama_tag)
        if not has_chat_template:
            return (
                "This GGUF has no embedded chat template - Ollama will fall "
                "back to raw completion (no chat formatting)."
            )
        return None
    if key == "lmstudio":
        link_lmstudio(gguf_path, repo_id)
        return None
    if key == "jan":
        link_jan(gguf_path, ollama_tag)
        return None
    if key == "anythingllm":
        link_ollama(gguf_path, ollama_tag, models_dir=anythingllm_ollama_models_dir())
        return None
    if key == "mstystudio":
        link_custom_directory(gguf_path, mstystudio_models_dir())
        return None
    if key == "textgenwebui":
        models_dir = textgenwebui_models_dir()
        if models_dir is None:
            raise LinkError("text-generation-webui not found.")
        link_custom_directory(gguf_path, models_dir)
        return None
    if key == "koboldcpp":
        models_dir = koboldcpp_models_dir()
        if models_dir is None:
            raise LinkError("KoboldCpp not found.")
        link_custom_directory(gguf_path, models_dir)
        return None
    raise ValueError(f"unknown engine: {key}")


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
