"""Find GGUF files sitting in the directories of every local AI app omm
knows about (or an arbitrary path) that aren't yet managed by omm, group
them by sha256 so identical copies collapse into one, and adopt the
survivors into the omm hub with a symlink (or, for manifest-based engines,
a rewritten manifest) left behind at every original location.

No Typer/console/questionary here so the scan/group/adopt logic stays
directly unit-testable - see cli.py for the interactive prompt flow that
drives this.
"""

from __future__ import annotations

import json
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from omm import linker, registry
from omm.config import MODELS_DIR, ensure_omm_home
from omm.hashutil import sha256_file
from omm.hub import ModelResolutionError, validate_model_filename

_OLLAMA_MODEL_LAYER = "application/vnd.ollama.image.model"

# Engines identified by a manifest/tag rather than a real on-disk filename
# (Ollama-format blobs, Jan's model.yml) - their display_name/path aren't a
# useful "canonical filename" for a model shared with a real-filename
# engine, so adopt_group() and ModelGroup.display_name prefer any other
# engine's name when one is available.
_MANIFEST_STYLE_ENGINES = {"ollama", "anythingllm"}


@dataclass
class ExternalGguf:
    engine: str  # one of linker.ENGINES' keys, or "import"
    display_name: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass
class ModelGroup:
    sha256: str
    locations: list[ExternalGguf]

    @property
    def size_bytes(self) -> int:
        return self.locations[0].size_bytes

    @property
    def display_name(self) -> str:
        for loc in self.locations:
            if loc.engine not in _MANIFEST_STYLE_ENGINES:
                return loc.display_name
        return self.locations[0].display_name

    @property
    def engines(self) -> list[str]:
        return sorted({loc.engine for loc in self.locations})


@dataclass
class AdoptResult:
    filename: str
    bytes_saved: int
    link_warnings: list[str]


def _is_safe_registry_filename(filename: object, resolver) -> bool:
    if not isinstance(filename, str):
        return False
    try:
        resolver(filename)
    except ModelResolutionError:
        return False
    return True


def _scan_ollama_format(engine: str, models_dir: Path) -> list[ExternalGguf]:
    """Real (non-symlink) model-layer blobs for any Ollama-format engine
    (system Ollama, or AnythingLLM's bundled instance at its own
    models_dir) - config/manifest blobs are skipped by only looking at
    digests actually referenced as a model layer, since every blob shares
    the same `sha256-<hash>` naming regardless of what it contains."""
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    if not blobs_dir.exists() or not manifests_root.exists():
        return []

    tags_by_digest: dict[str, list[str]] = {}
    for manifest_path in manifests_root.rglob("*"):
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        runtime_name = linker._ollama_runtime_name_from_manifest_path(
            manifest_path, manifests_root
        )
        if runtime_name is None:
            continue
        for layer in manifest.get("layers", []):
            if layer.get("mediaType") == _OLLAMA_MODEL_LAYER:
                digest = layer["digest"].removeprefix("sha256:")
                tags_by_digest.setdefault(digest, []).append(runtime_name)

    found = []
    for digest, tags in tags_by_digest.items():
        blob = blobs_dir / f"sha256-{digest}"
        if not blob.is_file() or blob.is_symlink():
            continue
        found.append(ExternalGguf(engine, tags[0], blob, blob.stat().st_size, digest))
    return found


def scan_ollama() -> list[ExternalGguf]:
    return _scan_ollama_format("ollama", linker.ollama_models_dir())


def scan_anythingllm() -> list[ExternalGguf]:
    return _scan_ollama_format("anythingllm", linker.anythingllm_ollama_models_dir())


def _scan_flat_dir(engine: str, base: Path) -> list[ExternalGguf]:
    """Real (non-symlink) .gguf files anywhere under `base` - shared by
    every engine that just recognizes a plain directory of GGUFs (LM
    Studio, Msty, text-generation-webui, KoboldCpp)."""
    if not base.exists():
        return []
    found = []
    for path in base.rglob("*.gguf"):
        if not path.is_file() or path.is_symlink():
            continue
        found.append(ExternalGguf(engine, path.name, path, path.stat().st_size, sha256_file(path)))
    return found


def scan_lmstudio() -> list[ExternalGguf]:
    return _scan_flat_dir("lmstudio", linker.lmstudio_models_dir())


def scan_mstystudio() -> list[ExternalGguf]:
    return _scan_flat_dir("mstystudio", linker.mstystudio_models_dir())


def scan_textgenwebui() -> list[ExternalGguf]:
    models_dir = linker.textgenwebui_models_dir()
    return _scan_flat_dir("textgenwebui", models_dir) if models_dir is not None else []


def scan_koboldcpp() -> list[ExternalGguf]:
    models_dir = linker.koboldcpp_models_dir()
    return _scan_flat_dir("koboldcpp", models_dir) if models_dir is not None else []


def scan_jan() -> list[ExternalGguf]:
    """Jan models are registered via a model.yml manifest whose model_path
    can be any absolute path (or one relative to Jan's data folder) - so,
    unlike the flat-directory engines, the actual .gguf here is very often
    outside Jan's own directory tree entirely."""
    models_dir = linker.jan_models_dir()
    if not models_dir.exists():
        return []
    jan_data_dir = linker.jan_app_dir() / "data"

    found = []
    for config_path in models_dir.glob("*/model.yml"):
        model_path_str = linker.read_jan_model_path(config_path)
        if not model_path_str:
            continue
        model_path = Path(model_path_str)
        if not model_path.is_absolute():
            model_path = jan_data_dir / model_path
        if not model_path.is_file() or model_path.is_symlink():
            continue
        found.append(
            ExternalGguf(
                "jan", config_path.parent.name, model_path, model_path.stat().st_size, sha256_file(model_path)
            )
        )
    return found


def scan_directory(path: Path) -> list[ExternalGguf]:
    found = []
    for gguf_path in path.rglob("*.gguf"):
        if not gguf_path.is_file() or gguf_path.is_symlink():
            continue
        found.append(
            ExternalGguf("import", gguf_path.name, gguf_path, gguf_path.stat().st_size, sha256_file(gguf_path))
        )
    return found


def find_external_models(extra_path: Path | None = None) -> list[ExternalGguf]:
    found = (
        scan_ollama()
        + scan_lmstudio()
        + scan_jan()
        + scan_anythingllm()
        + scan_mstystudio()
        + scan_textgenwebui()
        + scan_koboldcpp()
    )
    if extra_path is not None:
        found.extend(scan_directory(extra_path))
    return found


def group_by_hash(found: list[ExternalGguf]) -> list[ModelGroup]:
    by_hash: dict[str, list[ExternalGguf]] = {}
    for item in found:
        by_hash.setdefault(item.sha256, []).append(item)
    return [ModelGroup(h, locs) for h, locs in by_hash.items()]


def adopt_group(group: ModelGroup) -> AdoptResult:
    """Move one physical copy into the omm hub - or reuse an already
    hub-registered copy under this same sha256 - then replace every other
    location for this hash with a symlink to it. Returns bytes reclaimed."""
    ensure_omm_home()
    reg = registry.load_registry()
    def managed_path(filename: str) -> Path:
        filename = validate_model_filename(filename)
        path = MODELS_DIR / filename
        if not path.resolve().is_relative_to(MODELS_DIR.resolve()):
            raise ModelResolutionError("registry filename escapes the model hub")
        return path

    existing_filename = next(
        (
            fn
            for fn, entry in reg.items()
            if entry.get("sha256") == group.sha256
            and _is_safe_registry_filename(fn, managed_path)
        ),
        None,
    )

    linked = {spec.key: False for spec in linker.ENGINES}
    bytes_saved = 0
    discovered_ollama_runtime_name = next(
        (
            loc.display_name
            for loc in group.locations
            if loc.engine == "ollama" and loc.display_name
        ),
        None,
    )

    if existing_filename:
        hub_path = managed_path(existing_filename)
        linked.update(reg[existing_filename].get("linked", {}))
    else:
        preferred = next((loc for loc in group.locations if loc.engine not in _MANIFEST_STYLE_ENGINES), None)
        if preferred is not None:
            filename = validate_model_filename(
                unicodedata.normalize("NFC", preferred.path.name)
            )
        else:
            preferred = group.locations[0]
            filename = f"{linker.sanitize_ollama_tag(preferred.display_name)}.gguf"

        hub_path = MODELS_DIR / filename
        if hub_path.exists():
            hub_path = MODELS_DIR / f"{group.sha256[:12]}-{filename}"
        shutil.move(str(preferred.path), str(hub_path))

    for loc in group.locations:
        if loc.path.resolve() == hub_path.resolve():
            continue
        was_real_file = loc.path.is_file() and not loc.path.is_symlink()
        if was_real_file:
            # Keep the external copy recoverable until the replacement link
            # exists. A same-directory rename is atomic on Windows/NTFS and
            # avoids losing a model when a cross-drive hardlink fallback
            # fails after symlink creation is denied.
            quarantine = loc.path.with_name(f".{loc.path.name}.omm-import-{uuid.uuid4().hex}")
            loc.path.replace(quarantine)
            try:
                # `group.sha256` came from the earlier scan, so re-hash the
                # exact quarantined file at this destructive boundary.
                if sha256_file(quarantine) != sha256_file(hub_path):
                    raise linker.LinkError(
                        f"Refusing to replace changed unowned duplicate at {loc.path}."
                    )
                linker.link_file(hub_path, loc.path)
            except Exception:
                # link_file leaves no destination on its own failure paths.
                # Restore the original atomically rather than leaving a model
                # missing from its external engine directory.
                if not loc.path.exists() and not loc.path.is_symlink():
                    quarantine.replace(loc.path)
                raise
            else:
                quarantine.unlink()
        else:
            linker.link_file(hub_path, loc.path)
        if was_real_file:
            bytes_saved += loc.size_bytes
        if loc.engine in linked:
            linked[loc.engine] = True

    filename = hub_path.name
    if existing_filename:
        ollama_tag = reg[existing_filename].get("ollama_name") or linker.sanitize_ollama_tag(existing_filename)
        ollama_runtime_name = (
            reg[existing_filename].get("ollama_runtime_name")
            or discovered_ollama_runtime_name
        )
        repo_id = reg[existing_filename].get("repo_id")
    else:
        ollama_tag = linker.sanitize_ollama_tag(filename)
        ollama_runtime_name = discovered_ollama_runtime_name
        repo_id = None

    # The locations found by the scan only cover the engine(s) the file
    # already sat in - mirror `install`'s behavior of also linking into
    # every other currently-installed engine, so an imported model doesn't
    # end up under-linked compared to one added via `omm install`.
    link_warnings: list[str] = []
    for spec in linker.ENGINES:
        if linked.get(spec.key) or not linker.is_engine_installed(spec.key):
            continue
        try:
            warning = linker.link_engine(spec.key, hub_path, repo_id=repo_id, ollama_tag=ollama_tag)
            linked[spec.key] = True
            if warning:
                link_warnings.append(warning)
        except linker.LinkError as e:
            link_warnings.append(f"{spec.label} link skipped: {e}")

    if existing_filename:
        fields: dict[str, object] = {"linked": linked}
        if ollama_runtime_name:
            fields["ollama_runtime_name"] = ollama_runtime_name
        registry.upsert_entry(existing_filename, **fields)
    else:
        fields = dict(
            sha256=group.sha256,
            version=group.sha256[:7],
            source="imported",
            size_bytes=hub_path.stat().st_size,
            installed_at=datetime.now(timezone.utc).isoformat(),
            ollama_name=ollama_tag,
            repo_id=None,
            linked=linked,
        )
        if ollama_runtime_name:
            fields["ollama_runtime_name"] = ollama_runtime_name
        registry.upsert_entry(filename, **fields)

    return AdoptResult(filename=filename, bytes_saved=bytes_saved, link_warnings=link_warnings)
