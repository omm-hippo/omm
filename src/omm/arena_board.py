"""Reading the published arena leaderboard (sub-project D, issue #410).

`omm arena --leaderboard` shows how people with machines like this one rated
the models they battled. The board is computed nightly from uploaded votes
(scripts/aggregate_arena_votes.py), Ed25519-signed, and published to `main`;
this module fetches it, verifies the signature before anything is shown,
caches the verified bytes for offline use, picks the scope to show, and
numbers the rows so `omm install 2` / `omm arena 1 3` work afterwards.

Network and verification problems surface as the exceptions below; the CLI
decides what to print. No ranking is ever returned from bytes whose
signature did not verify - live or cached.

Spec: docs/superpowers/specs/2026-10-16-arena-public-leaderboard-design.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import catalog, config, memory_group, session_cache
from .atomic import atomic_write_bytes, atomic_write_text

#: The artifact layout this client understands (memory groups + pairs).
SUPPORTED_SCHEMA = 2

#: A numbered row for a model the votes named without a source repo. Kept in
#: the session cache so the number still means something, and turned into a
#: plain "can't install this from the leaderboard" by cli._resolve_ref.
UNSOURCED_REF_PREFIX = session_cache.UNSOURCED_REF_PREFIX

MAX_BOARD_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024

OVERALL_LABEL = "All machines"


class BoardError(Exception):
    """Base for the reasons no ranking can be shown."""


class BoardVerificationError(BoardError):
    """The published bytes did not verify against the trusted key."""


class BoardUnavailable(BoardError):
    """Could not fetch the board, and there is no cached copy that verifies."""


class BoardTooNew(BoardError):
    """The board uses a newer layout than this omm understands."""


def _cache_paths() -> tuple[Path, Path]:
    """Resolved at call time; OMM_HOME is overridable."""
    directory = config.OMM_HOME / "arena"
    return directory / "leaderboard.json", directory / "leaderboard.manifest.json"


def _get(url: str, *, maximum: int, label: str) -> bytes | None:
    """Body bytes, or None on a 404. Raises requests.RequestException or
    ValueError (oversized) for everything the caller treats as "offline"."""
    import requests

    from .httpjson import bounded_response_bytes

    response = requests.get(url, timeout=15, stream=True)
    try:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return bounded_response_bytes(response, maximum=maximum, label=label)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _verified(raw: bytes, manifest_raw: bytes | None, public_key: str) -> dict:
    """Parse `raw` only after its signature verified; enforce the schema."""
    if manifest_raw is None:
        raise BoardVerificationError("the leaderboard has no signature manifest")
    try:
        manifest = json.loads(manifest_raw)
        catalog.verify_signed_artifact(raw, manifest, public_key)
    except (ValueError, TypeError) as error:
        # CatalogVerificationError is a ValueError; so is malformed JSON.
        raise BoardVerificationError(str(error) or "signature check failed") from error
    try:
        artifact = json.loads(raw)
    except ValueError as error:
        raise BoardVerificationError("the signed leaderboard is not JSON") from error
    if not isinstance(artifact, dict):
        raise BoardVerificationError("the signed leaderboard is not a JSON object")
    version = artifact.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SUPPORTED_SCHEMA:
        raise BoardTooNew(f"leaderboard schema {version} is newer than this omm reads")
    if version != SUPPORTED_SCHEMA:
        raise BoardVerificationError(f"unexpected leaderboard schema {version!r}")
    return artifact


def _load_cache(public_key: str) -> dict:
    artifact_path, manifest_path = _cache_paths()
    try:
        if artifact_path.stat().st_size > MAX_BOARD_BYTES:
            raise BoardUnavailable("the cached leaderboard is too large")
        raw = artifact_path.read_bytes()
        manifest_raw = manifest_path.read_bytes()
    except OSError as error:
        raise BoardUnavailable("couldn't reach the leaderboard and none is cached") from error
    try:
        return _verified(raw, manifest_raw, public_key)
    except BoardVerificationError as error:
        raise BoardUnavailable(
            "couldn't reach the leaderboard, and the cached copy no longer verifies"
        ) from error


def _save_cache(raw: bytes, manifest_raw: bytes) -> None:
    artifact_path, manifest_path = _cache_paths()
    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(artifact_path, raw)
        atomic_write_text(manifest_path, manifest_raw.decode("utf-8"))
    except (OSError, UnicodeError):
        pass


def fetch_board(public_key: str) -> dict | None:
    """The verified leaderboard artifact, or None when none is published yet
    (404 - before the arena work reaches `main` and the first nightly run).

    Live first; on a network failure, the last cached copy if it still
    verifies. A live board that fails verification is an error, never a
    reason to fall back to an older cached one.
    """
    import requests

    try:
        raw = _get(config.ARENA_LEADERBOARD_URL, maximum=MAX_BOARD_BYTES, label="arena leaderboard")
        if raw is None:
            return None
        manifest_raw = _get(
            config.ARENA_LEADERBOARD_MANIFEST_URL,
            maximum=MAX_MANIFEST_BYTES,
            label="arena leaderboard manifest",
        )
    except (requests.RequestException, ValueError):
        return _load_cache(public_key)
    artifact = _verified(raw, manifest_raw, public_key)
    _save_cache(raw, manifest_raw or b"")
    return artifact


def group_label(key: str) -> str:
    """Human label for a scope key ("overall" or "<kind>:<bucket>"); an
    unknown key (a bucket table this client predates) shows as itself."""
    if key == "overall":
        return OVERALL_LABEL
    kind, _sep, bucket = key.partition(":")
    return memory_group.display_label(kind, bucket)


_SCOPE_FIELDS = (
    "corpus", "models", "tiers", "efficiency_components", "provisional_models", "pairs",
    "ranked", "reasons", "updated_at",
)


@dataclass(frozen=True)
class Selection:
    scope: dict
    scope_key: str
    scope_label: str
    machine_group: tuple[str, str] | None
    machine_label: str | None
    #: The machine's own group entry, ranked or not - its vote counts are what
    #: a "collecting" or fallback message shows. None when it has no entry.
    machine_scope: dict | None
    #: True when the machine has a group but it is not ranked (or not on the
    #: board yet), so the overall board is shown instead.
    fell_back: bool


def select_scope(artifact: dict, machine_group: tuple[str, str] | None) -> Selection:
    overall = {key: artifact.get(key) for key in _SCOPE_FIELDS}
    machine_scope = None
    machine_label = None
    if machine_group is not None:
        key = f"{machine_group[0]}:{machine_group[1]}"
        machine_label = memory_group.display_label(*machine_group)
        for group in artifact.get("groups") or []:
            if isinstance(group, dict) and group.get("key") == key:
                machine_scope = group
                break
        if machine_scope is not None and machine_scope.get("ranked") is True:
            return Selection(
                scope=machine_scope,
                scope_key=key,
                scope_label=machine_label,
                machine_group=machine_group,
                machine_label=machine_label,
                machine_scope=machine_scope,
                fell_back=False,
            )
    return Selection(
        scope=overall,
        scope_key="overall",
        scope_label=OVERALL_LABEL,
        machine_group=machine_group,
        machine_label=machine_label,
        machine_scope=machine_scope,
        fell_back=machine_group is not None,
    )


@dataclass(frozen=True)
class BoardRow:
    number: int
    model: dict
    installed_filename: str | None
    ref: str


def _installed_filename(model: dict, by_digest: dict, by_name: dict) -> str | None:
    key = model.get("key")
    if isinstance(key, str) and key.startswith("sha256:"):
        found = by_digest.get(key.removeprefix("sha256:"))
        if found is not None:
            return found
    names = [model.get("display_filename")]
    if isinstance(key, str) and key.startswith("filename:"):
        names.append(key.removeprefix("filename:"))
    for name in names:
        if isinstance(name, str) and name.lower() in by_name:
            return by_name[name.lower()]
    return None


def numbered_rows(scope: dict, registry_entries: dict) -> list[BoardRow]:
    """Ranked (non-provisional) models in board order, numbered from 1 across
    tiers, each with the ref its number should resolve to: the registry
    filename when installed (so `omm arena` / `omm info` work), an exact
    install ref otherwise, or an unsourced marker when the votes never named
    a source repo."""
    from . import search

    by_digest: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for filename, entry in (registry_entries or {}).items():
        if not isinstance(filename, str):
            continue
        by_name.setdefault(filename.lower(), filename)
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if isinstance(digest, str):
            by_digest.setdefault(digest.strip().lower().removeprefix("sha256:"), filename)

    rows: list[BoardRow] = []
    for model in scope.get("models") or []:
        if not isinstance(model, dict) or model.get("provisional", True):
            continue
        installed = _installed_filename(model, by_digest, by_name)
        display = model.get("display_filename") or str(model.get("key", ""))
        if installed is not None:
            ref = installed
        elif model.get("repo_id"):
            ref = search.exact_install_ref(
                {
                    "repo_id": model["repo_id"],
                    "filename": display,
                    "provider": model.get("provider"),
                }
            )
        else:
            ref = UNSOURCED_REF_PREFIX + display
        rows.append(BoardRow(number=len(rows) + 1, model=model, installed_filename=installed, ref=ref))
    return rows
