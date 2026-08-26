"""Read-only installation state for ``omm recommend`` candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from omm import config, hub, linker, registry, scan_import
from omm.hub import ModelResolutionError, validate_model_filename


MatchKind = Literal["exact", "model_identity"]


@dataclass(frozen=True)
class InstallationStatus:
    installed: bool = False
    managed_by_omm: bool = False
    engines: tuple[str, ...] = ()
    managed_filename: str | None = None
    match_kind: MatchKind | None = None


NOT_INSTALLED = InstallationStatus()


def _normalized(value: object) -> str:
    return str(value).strip().replace("\\", "/").casefold()


def _candidate_coordinates(candidate: dict) -> tuple[str | None, str | None, str | None]:
    """Return provider/repository/filename, expanding curated rule names."""

    name = candidate.get("name")
    curated = hub.CURATED_INDEX.get(name) if isinstance(name, str) else None
    repo_id = candidate.get("repo_id") or (curated[0] if curated else None)
    filename = candidate.get("filename") or (curated[1] if curated else None)
    provider = candidate.get("provider") or ("huggingface" if repo_id else None)
    return provider, repo_id, filename


def _managed_file_exists(filename: str) -> bool:
    try:
        safe_filename = validate_model_filename(filename)
        path = config.MODELS_DIR / safe_filename
        if not path.resolve().is_relative_to(config.MODELS_DIR.resolve()):
            return False
        return path.is_file()
    except (ModelResolutionError, OSError):
        return False


def _managed_status(candidate: dict, reg: dict) -> InstallationStatus | None:
    provider, repo_id, candidate_filename = _candidate_coordinates(candidate)
    if not repo_id or not candidate_filename:
        return None

    expected_repo = _normalized(repo_id)
    expected_provider = _normalized(provider)
    try:
        expected_filename = hub.model_filename_identity(candidate_filename)
    except ModelResolutionError:
        return None

    for filename, entry in reg.items():
        if not isinstance(entry, dict) or not isinstance(filename, str):
            continue
        try:
            same_filename = hub.model_filename_identity(filename) == expected_filename
        except ModelResolutionError:
            continue
        if not same_filename or _normalized(entry.get("repo_id")) != expected_repo:
            continue
        saved_provider = entry.get("provider")
        if saved_provider and _normalized(saved_provider) != expected_provider:
            continue
        if not _managed_file_exists(filename):
            continue
        linked = entry.get("linked")
        engines = tuple(
            spec.key
            for spec in linker.ENGINES
            if isinstance(linked, dict) and linked.get(spec.key) is True
        )
        return InstallationStatus(True, True, engines, filename, "exact")
    return None


def _managed_model_identity_status(
    candidate: dict, reg: dict
) -> InstallationStatus | None:
    """Match an imported/legacy OMM model by bounded model identity.

    Older imports often have no repository provenance and use an Ollama-style
    filename such as ``qwen3.5-9b.gguf``.  They cannot exact-match the hosted
    GGUF filename, but they are still a real local installation of that model.
    Entries with known, different repository provenance remain distinct.
    """

    candidate_identities = _candidate_semantic_identities(candidate)
    if not candidate_identities:
        return None
    _provider, candidate_repo, _filename = _candidate_coordinates(candidate)
    expected_repo = _normalized(candidate_repo)

    for filename, entry in reg.items():
        if not isinstance(entry, dict) or not isinstance(filename, str):
            continue
        saved_repo = entry.get("repo_id")
        if saved_repo and _normalized(saved_repo) != expected_repo:
            continue
        if not _managed_file_exists(filename):
            continue
        values = {
            filename,
            entry.get("ollama_name"),
            entry.get("ollama_runtime_name"),
        }
        installed_identities = {
            identity
            for value in values
            if value and (identity := _semantic_model_identity(value)) is not None
        }
        if not candidate_identities & installed_identities:
            continue
        linked = entry.get("linked")
        engines = tuple(
            spec.key
            for spec in linker.ENGINES
            if isinstance(linked, dict) and linked.get(spec.key) is True
        )
        return InstallationStatus(
            True,
            True,
            engines,
            filename,
            "model_identity",
        )
    return None


def _runtime_aliases(candidate: dict) -> set[str]:
    _provider, repo_id, filename = _candidate_coordinates(candidate)
    values = {
        candidate.get("name"),
        repo_id,
        PurePosixPath(str(repo_id)).name if repo_id else None,
        filename,
        PurePosixPath(str(filename)).name if filename else None,
    }
    if filename:
        basename = PurePosixPath(str(filename)).name
        stem = basename[: -len(".gguf")] if basename.casefold().endswith(".gguf") else basename
        tag = linker.sanitize_ollama_tag(str(filename))
        values.update({stem, tag, f"{tag}:latest"})
    aliases = {_normalized(value) for value in values if value}
    aliases.update(
        value[: -len(":latest")]
        for value in tuple(aliases)
        if value.endswith(":latest")
    )
    return aliases


def _path_has_suffix(path_parts: tuple[str, ...], suffix: tuple[str, ...]) -> bool:
    return len(path_parts) >= len(suffix) and path_parts[-len(suffix) :] == suffix


def _semantic_model_identity(value: object) -> tuple[str, ...] | None:
    """Normalize packaging syntax while keeping the model identity exact.

    This intentionally does not remove role variants such as ``coder`` or
    ``instruct``.  It only removes a terminal quantization/package suffix,
    and splits letter/number boundaries so ``qwen3.5:9b`` and
    ``Qwen3.5-9B-Q4_K_M.gguf`` resolve to the same bounded identity.
    """

    tokens = re.findall(
        r"(?:i?u?q[1-8])|(?:bf16|fp16|f16|fp32|f32)|"
        r"\d+(?:\.\d+)?[ab]?|[a-z]+",
        _normalized(value),
    )
    kept = []
    for token in tokens:
        if token == "gguf":
            continue
        if re.fullmatch(r"(?:i?u?q[1-8]|bf16|fp16|f16|fp32|f32)", token):
            if kept and kept[-1] == "ud":
                kept.pop()
            break
        kept.append(token)
    if len(kept) < 2 or not any(re.fullmatch(r"\d+(?:\.\d+)?b", token) for token in kept):
        return None
    return tuple(kept)


def _candidate_semantic_identities(candidate: dict) -> set[tuple[str, ...]]:
    _provider, repo_id, filename = _candidate_coordinates(candidate)
    values = {
        candidate.get("name"),
        PurePosixPath(str(repo_id)).name if repo_id else None,
        PurePosixPath(str(filename)).name if filename else None,
    }
    return {
        identity
        for value in values
        if value and (identity := _semantic_model_identity(value)) is not None
    }


def _external_match_kind(
    candidate: dict, identity: scan_import.ExternalModelIdentity
) -> MatchKind | None:
    _provider, repo_id, filename = _candidate_coordinates(candidate)
    if not filename:
        return None

    path_parts = tuple(_normalized(part) for part in identity.path.parts)
    filename_parts = tuple(
        _normalized(part) for part in PurePosixPath(str(filename)).parts
    )
    if filename_parts and _path_has_suffix(path_parts, filename_parts):
        return "exact"

    if repo_id:
        repo_parts = tuple(
            _normalized(part) for part in PurePosixPath(str(repo_id)).parts
        )
        if _path_has_suffix(path_parts, (*repo_parts, *filename_parts)):
            return "exact"

    available = {
        _normalized(identity.display_name),
        _normalized(identity.path.name),
    }
    if identity.engine in {"ollama", "anythingllm"}:
        available.update(
            value[: -len(":latest")]
            for value in tuple(available)
            if value.endswith(":latest")
        )
        if _runtime_aliases(candidate) & available:
            return "exact"
        runtime_identity = _semantic_model_identity(identity.display_name)
        if (
            runtime_identity is not None
            and runtime_identity in _candidate_semantic_identities(candidate)
        ):
            return "model_identity"
        return None

    # Flat-file runtimes use the GGUF filename itself as their exact local
    # identifier.  No family-name or substring matching is permitted.
    return (
        "exact"
        if _normalized(PurePosixPath(str(filename)).name) in available
        else None
    )


def detect_installation_statuses(candidates: list[dict]) -> list[InstallationStatus]:
    """Classify candidates using local persisted state and exact identifiers.

    Runtime discovery is best-effort: an unreadable or absent external app
    must not make hardware recommendation itself fail.
    """

    reg = registry.load_registry()
    try:
        external = scan_import.find_external_model_identities()
    except (KeyError, OSError, TypeError, ValueError):
        external = []

    statuses = []
    for candidate in candidates:
        managed = _managed_status(candidate, reg)
        if managed is not None:
            statuses.append(managed)
            continue
        managed_identity = _managed_model_identity_status(candidate, reg)
        if managed_identity is not None:
            statuses.append(managed_identity)
            continue
        matches = {
            spec.key: tuple(
                match_kind
                for identity in external
                if identity.engine == spec.key
                if (match_kind := _external_match_kind(candidate, identity)) is not None
            )
            for spec in linker.ENGINES
        }
        engines = tuple(spec.key for spec in linker.ENGINES if matches[spec.key])
        match_kind = (
            "exact"
            if any("exact" in engine_matches for engine_matches in matches.values())
            else "model_identity" if engines else None
        )
        statuses.append(
            NOT_INSTALLED
            if not engines
            else InstallationStatus(
                True,
                False,
                engines,
                match_kind=match_kind,
            )
        )
    return statuses
