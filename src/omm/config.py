"""Central paths and user config (~/.omm)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from omm.atomic import atomic_write_text, backup_corrupt_file, locked

def _resolve_omm_home() -> Path:
    """~/.omm by default; OMM_HOME overrides it for machines where the home
    directory's filesystem lacks room for GGUF models (e.g. contribute)."""
    override = os.environ.get("OMM_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omm"


OMM_HOME = _resolve_omm_home()
MODELS_DIR = OMM_HOME / "models"
CONFIG_PATH = OMM_HOME / "config.json"
REGISTRY_PATH = OMM_HOME / "models.json"
# Records only Windows hard links created by omm.  Unlike a symlink, a hard
# link is indistinguishable from an ordinary file, so cleanup must have an
# explicit ownership record before it is allowed to remove one.
LINK_OWNERSHIP_PATH = OMM_HOME / "link-ownership.json"
RULES_PATH = OMM_HOME / "rules.json"
RECOMMEND_MODEL_PATH = OMM_HOME / "recommend-model.json"
EVALUATIONS_DIR = OMM_HOME / "evaluations"
CALIBRATION_PATH = OMM_HOME / "calibration.json"
CATALOG_HISTORY_DIR = OMM_HOME / "catalog-history"
LEGACY_FIREBASE_ENDPOINT = (
    "https://localfit-8ab57-default-rtdb.firebaseio.com/telemetry.json"
)
# The RTDB `telemetry/$event` node now denies direct client writes (see
# omm-hippo/omm#133): the Cloudflare Worker gateway is the only writer,
# gated by proof-of-work instead of by an unlimited-mintable anonymous auth
# token. `_post_event` detects this URL and POSTs {event_json, timestamp,
# nonce} to it instead of writing to Firebase directly - see omm.telemetry.
TELEMETRY_GATEWAY_ENDPOINT = "https://omm-telemetry-gateway.seong381400.workers.dev/telemetry"
# error_report.endpoint() normally derives the error-report URL from
# telemetry_endpoint by rewriting its last path segment - but that trick
# assumes both channels live on the same host, which stopped being true the
# moment telemetry_endpoint started pointing at the gateway instead of
# Firebase directly (omm-hippo/omm#133 is telemetry-only; error_reports
# still writes straight to Firebase with a client auth token, unaffected).
# error_report.endpoint() special-cases TELEMETRY_GATEWAY_ENDPOINT to this
# constant instead of rewriting it, so the default error-report destination
# is unchanged from before the migration.
ERROR_REPORTS_ENDPOINT = "https://localfit-8ab57-default-rtdb.firebaseio.com/error_reports.json"
# Public client identifier for the `localfit-8ab57` Firebase project - not a
# secret. Firebase Web API keys are safe to ship in client code (they only
# identify the project to Google's Identity Toolkit; actual access is
# governed by the RTDB security rules, not this key). Used solely to sign in
# anonymously so telemetry writes carry `auth != null`, as the RTDB rules
# require - see omm.firebase_auth.
FIREBASE_WEB_API_KEY = "AIzaSyBlnr7Qhu4H4z93X1jUpJDyuNz4D5tyca4"
# model_url has gone through two GitHub org renames (minigu5/Localfit ->
# minigu5/Omm -> omm-hippo/omm) plus one artifact rename (recommend-model.json
# -> localfit-recommend-model.json). It's never user-settable, so any config
# still holding one of the earlier defaults is stale, not a deliberate
# override, and should be migrated forward.
LEGACY_MODEL_URLS = frozenset(
    {
        "https://raw.githubusercontent.com/minigu5/Localfit/main/published/recommend-model.json",
        "https://raw.githubusercontent.com/minigu5/Omm/main/published/recommend-model.json",
        "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.json",
    }
)
# Same idea as LEGACY_MODEL_URLS, for the signed manifest URL after the
# recommend-model.json -> localfit-recommend-model.json artifact rename.
LEGACY_MANIFEST_URLS = frozenset(
    {
        "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json",
    }
)

DEFAULT_CONFIG: dict[str, Any] = {
    "telemetry_send_policy": "ask",
    # New installs point at the PoW-gated Cloudflare Worker gateway by
    # default (see TELEMETRY_GATEWAY_ENDPOINT). Existing configs already
    # migrated to local-only or the legacy direct-Firebase endpoint are
    # handled in _merge_config - this only affects users with no
    # config.json yet. Teams may still point telemetry_endpoint at the
    # bundled FastAPI server.
    "telemetry_endpoint": TELEMETRY_GATEWAY_ENDPOINT,
    "telemetry_backend": "gateway",
    # Error auto-reporting is strictly opt-in, so the effective default is
    # "never" (see omm.error_report.send_policy). It is stored as None
    # rather than the literal "never" so that "the user has never chosen"
    # stays distinguishable from "the user chose never": only the latter
    # makes `omm contribute --report-errors` a no-op with a guidance
    # message, since an explicit opt-out must not be overridable by a
    # runtime flag.
    "error_report_send_policy": None,
    "rules_url": None,
    "model_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/localfit-recommend-model.json",
    "default_engine": None,
    "external_scan_done": False,
    "catalog_manifest_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/localfit-recommend-model.manifest.json",
    "catalog_public_key": "p8uo6GFXDcg8Rp7/t8GGl5hwPsXhObY5vI1sll5KpaI=",
    "contribute_always_ack": False,
    "update_channel": "stable",
    "onboarding_completed": True,
    "theme": "dark",
    "memory_guard_policy": "ask",
    "memory_guard_poll_seconds": 1.0,
    "memory_guard_low_memory_seconds": 3.0,
    # Cumulative bytes reclaimed by `omm import`'s dedup step (a real
    # duplicate .gguf replaced with a symlink into the hub). `omm link`
    # never duplicates a file in the first place, so it has nothing to add
    # here - see scan_import.adopt_group's bytes_saved.
    "storage_saved_bytes": 0,
}


def ensure_omm_home() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _merge_config(data: dict[str, Any]) -> dict[str, Any]:
    if "telemetry_send_policy" not in data and "telemetry_opt_in" in data:
        data = {
            **data,
            "telemetry_send_policy": "always" if data["telemetry_opt_in"] else "ask",
        }
    merged = {**DEFAULT_CONFIG, **data}
    merged.pop("telemetry_opt_in", None)
    if data.get("catalog_manifest_url") is None and data.get("catalog_public_key") is None:
        # Configs written before this branch always had these two keys
        # explicitly set to null (they've been in DEFAULT_CONFIG since an
        # earlier commit). There is currently no user-facing way to
        # explicitly clear them back to null, so seeing both null here means
        # "pre-signing config", not "user opted out" - migrate it forward to
        # the new signed-by-default catalog. A real (non-None) value in
        # either key means the user has run `omm setting catalog-trust` and
        # their custom value must win, so this migration is skipped then.
        merged["catalog_manifest_url"] = DEFAULT_CONFIG["catalog_manifest_url"]
        merged["catalog_public_key"] = DEFAULT_CONFIG["catalog_public_key"]
    if data.get("model_url") in LEGACY_MODEL_URLS:
        merged["model_url"] = DEFAULT_CONFIG["model_url"]
    if data.get("catalog_manifest_url") in LEGACY_MANIFEST_URLS:
        merged["catalog_manifest_url"] = DEFAULT_CONFIG["catalog_manifest_url"]
    if "telemetry_backend" not in data:
        endpoint = data.get("telemetry_endpoint")
        if endpoint == LEGACY_FIREBASE_ENDPOINT and merged.get("telemetry_send_policy") != "always":
            merged["telemetry_endpoint"] = None
            merged["telemetry_backend"] = "local"
        elif isinstance(endpoint, str) and "firebaseio.com" in endpoint:
            merged["telemetry_backend"] = "firebase_legacy"
        elif endpoint:
            merged["telemetry_backend"] = "self_hosted"
    # The direct-Firebase endpoint now rejects every write (omm-hippo/omm#133
    # closed telemetry/$event to anything but the Cloudflare gateway) - any
    # config still pointed at it, from any era, must move forward or its
    # telemetry silently stops landing anywhere.
    if merged.get("telemetry_backend") == "firebase_legacy" and merged.get("telemetry_endpoint") == LEGACY_FIREBASE_ENDPOINT:
        merged["telemetry_endpoint"] = TELEMETRY_GATEWAY_ENDPOINT
        merged["telemetry_backend"] = "gateway"
    if merged.get("memory_guard_policy") not in {"ask", "block", "observe"}:
        merged["memory_guard_policy"] = "ask"
    poll_seconds = merged.get("memory_guard_poll_seconds")
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not 0.1 <= poll_seconds <= 60
    ):
        merged["memory_guard_poll_seconds"] = 1.0
    low_seconds = merged.get("memory_guard_low_memory_seconds")
    if (
        isinstance(low_seconds, bool)
        or not isinstance(low_seconds, (int, float))
        or not 0 <= low_seconds <= 300
    ):
        merged["memory_guard_low_memory_seconds"] = 3.0
    saved_bytes = merged.get("storage_saved_bytes")
    if isinstance(saved_bytes, bool) or not isinstance(saved_bytes, (int, float)) or saved_bytes < 0:
        merged["storage_saved_bytes"] = 0
    return merged


def load_config() -> dict[str, Any]:
    ensure_omm_home()
    if not CONFIG_PATH.exists():
        fresh = {**DEFAULT_CONFIG, "onboarding_completed": False}
        save_config(fresh)
        return fresh
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup_corrupt_file(CONFIG_PATH)
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        backup_corrupt_file(CONFIG_PATH)
        return dict(DEFAULT_CONFIG)
    return _merge_config(data)


def save_config(config: dict[str, Any]) -> None:
    ensure_omm_home()
    with locked(CONFIG_PATH):
        atomic_write_text(CONFIG_PATH, json.dumps(config, indent=2) + "\n")


def update_config(**changes: Any) -> dict[str, Any]:
    """Merge a small update while serializing the complete read/write cycle."""
    ensure_omm_home()
    with locked(CONFIG_PATH):
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    backup_corrupt_file(CONFIG_PATH)
            except (OSError, ValueError):
                backup_corrupt_file(CONFIG_PATH)
        current = _merge_config(data)
        current.update(changes)
        atomic_write_text(CONFIG_PATH, json.dumps(current, indent=2) + "\n")
    return current


def add_storage_saved_bytes(delta: int) -> int:
    """Atomically add to the cumulative `storage_saved_bytes` counter and
    return the new total. Read-modify-write happens under the same lock as
    `update_config`, so two `omm import` runs racing each other (e.g. one
    started by `_maybe_auto_import` from a concurrent session) can't clobber
    each other's contribution the way a plain `update_config(storage_saved_bytes=...)`
    computed from a value read outside the lock could."""
    ensure_omm_home()
    with locked(CONFIG_PATH):
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    backup_corrupt_file(CONFIG_PATH)
            except (OSError, ValueError):
                backup_corrupt_file(CONFIG_PATH)
        current = _merge_config(data)
        total = int(current["storage_saved_bytes"]) + delta
        current["storage_saved_bytes"] = total
        atomic_write_text(CONFIG_PATH, json.dumps(current, indent=2) + "\n")
    return total
