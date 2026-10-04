"""Read and verify the public arena artifact; never read private votes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from omm import catalog, config
from omm.atomic import atomic_write_text, locked

ARTIFACT_URL = "https://raw.githubusercontent.com/omm-hippo/omm/main/published/arena-leaderboard.json"
MANIFEST_URL = "https://raw.githubusercontent.com/omm-hippo/omm/main/published/arena-leaderboard.manifest.json"
MAX_BYTES = 4 * 1024 * 1024
MAX_MODELS = 4096


class LeaderboardError(ValueError):
    pass


def read_public_key():
    """Read the configured trust anchor without migrating or creating config."""
    try:
        if config.CONFIG_PATH.stat().st_size > 1024 * 1024:
            raise LeaderboardError("Configuration is too large")
        data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LeaderboardError("Invalid configuration")
        return data.get("catalog_public_key", config.DEFAULT_CONFIG["catalog_public_key"])
    except FileNotFoundError:
        return config.DEFAULT_CONFIG["catalog_public_key"]


def _number(value, label, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise LeaderboardError(f"Invalid leaderboard {label}")
    return value


def _text(value, label, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 400 or any(ord(c) < 32 for c in value):
        raise LeaderboardError(f"Invalid leaderboard {label}")


def validate(document: object) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise LeaderboardError("Unsupported leaderboard schema")
    _text(document.get("generated_at"), "generated_at")
    try:
        generated = datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as error:
        raise LeaderboardError("Invalid leaderboard timestamp") from error
    corpus = document.get("corpus")
    if not isinstance(corpus, dict):
        raise LeaderboardError("Missing leaderboard corpus")
    for field in ("rows_fetched", "rows_used", "effective_votes", "client_count", "largest_client_share"):
        _number(corpus.get(field), field)
    if corpus["largest_client_share"] > 1:
        raise LeaderboardError("Invalid client share")
    models = document.get("models")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise LeaderboardError("Leaderboard models must be a bounded list")
    seen = set()
    for model in models:
        if not isinstance(model, dict):
            raise LeaderboardError("Leaderboard model must be an object")
        key = model.get("key")
        _text(key, "model key")
        if not (re.fullmatch(r"sha256:[0-9a-f]{64}", key) or key.startswith("filename:")) or key in seen:
            raise LeaderboardError("Invalid or duplicate leaderboard model key")
        seen.add(key)
        for field in ("display_filename", "repo_id", "provider"):
            _text(model.get(field), field, optional=True)
        _number(model.get("battles"), "battles")
        _number(model.get("effective_battles"), "effective battles")
        if type(model.get("provisional")) is not bool or type(model.get("quality_warning")) is not bool:
            raise LeaderboardError("Invalid model status")
        if _number(model.get("both_bad_rate"), "both-bad rate") > 1:
            raise LeaderboardError("Invalid both-bad rate")
        quality = model.get("quality")
        if not isinstance(quality, dict):
            raise LeaderboardError("Missing model quality")
        for field in ("strength", "ci_low", "ci_high"):
            # The producer exports log-strengths, including negative values.
            _number(quality.get(field), field, minimum=-math.inf)
        if quality["ci_low"] > quality["ci_high"]:
            raise LeaderboardError("Inverted quality interval")
        tier = quality.get("tier")
        if tier is not None and (type(tier) is not int or tier < 1):
            raise LeaderboardError("Invalid quality tier")
        if model["provisional"] != (tier is None):
            raise LeaderboardError("Provisional models cannot have a ranked tier")
        measured = model.get("efficiency")
        if measured is not None:
            if not isinstance(measured, dict) or type(measured.get("component")) is not int or measured["component"] < 0:
                raise LeaderboardError("Invalid efficiency comparison group")
            _number(measured.get("rating"), "efficiency rating", minimum=-1e6)
            _number(measured.get("raw_median_tok_s_per_gb"), "raw efficiency")
    return document


def verified(content: bytes, manifest: object, public_key: str) -> dict:
    if len(content) > MAX_BYTES:
        raise LeaderboardError("Leaderboard is too large")
    try:
        catalog.verify_signed_artifact(content, manifest, public_key)
        return validate(json.loads(content))
    except (catalog.CatalogVerificationError, ValueError, TypeError, UnicodeError) as error:
        raise LeaderboardError(f"Leaderboard verification failed: {error}") from error


def ordered_models(document: dict) -> list[dict]:
    """Efficiency orders only a tier's members in the same comparison group."""
    def order(model):
        efficiency = model.get("efficiency")
        return (
            model["quality"]["tier"] if not model["provisional"] else math.inf,
            efficiency["component"] if efficiency else math.inf,
            -efficiency["rating"] if efficiency else 0,
            model["key"],
        )
    return sorted(document["models"], key=order)


def _download(url: str, maximum: int) -> bytes:
    import requests
    try:
        with requests.get(url, stream=True, timeout=(5, 15), allow_redirects=False) as response:
            if response.status_code == 404:
                raise LeaderboardError("No public signed arena leaderboard has been published yet.")
            response.raise_for_status()
            chunks = []
            length = 0
            for chunk in response.iter_content(64 * 1024):
                length += len(chunk)
                if length > maximum:
                    raise LeaderboardError("Leaderboard response is too large")
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException as error:
        raise LeaderboardError("Could not fetch the public leaderboard") from error


def load(public_key: str, *, offline: bool = False) -> tuple[dict, bool]:
    """Cache the signed pair in one atomic envelope, avoiding torn cache writes."""
    try:
        catalog.load_public_key(public_key)
    except (catalog.CatalogVerificationError, TypeError) as error:
        raise LeaderboardError("Invalid configured leaderboard public key") from error
    path = config.OMM_HOME / "arena" / "leaderboard-cache.json"
    old = None
    try:
        if path.stat().st_size <= MAX_BYTES * 2:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            old = verified(envelope["content"].encode("utf-8"), envelope["manifest"], public_key)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    if offline:
        if old is None:
            raise LeaderboardError("No verified cached leaderboard is available in offline mode.")
        return old, True
    try:
        content = _download(ARTIFACT_URL, MAX_BYTES)
        manifest = json.loads(_download(MANIFEST_URL, 16 * 1024))
        document = verified(content, manifest, public_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked(path):
            atomic_write_text(path, json.dumps({"content": content.decode("utf-8"), "manifest": manifest}, ensure_ascii=False))
        return document, False
    except (LeaderboardError, OSError, ValueError):
        if old is not None:
            return old, True
        raise


def age_days(document: dict) -> int:
    generated = datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))
    return max(0, (datetime.now(timezone.utc) - generated).days)
