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

DEFAULT_CONFIG: dict[str, Any] = {
    "telemetry_send_policy": "ask",
    # New installs point at our hosted Firebase collector by default. Existing
    # configs that were already migrated to local-only (see _merge_config)
    # are left untouched - this only affects users with no config.json yet.
    # Teams may still point telemetry_endpoint at the bundled FastAPI server.
    "telemetry_endpoint": LEGACY_FIREBASE_ENDPOINT,
    "telemetry_backend": "firebase_legacy",
    "rules_url": None,
    "model_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.json",
    "default_engine": None,
    "external_scan_done": False,
    "catalog_manifest_url": "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json",
    "catalog_public_key": "z6tdGIAocCKvH/dREXOaSe50uQ5TJo26mWEG5JYwVqY=",
    "contribute_always_ack": False,
    "update_channel": "stable",
    "onboarding_completed": True,
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
    if "telemetry_backend" not in data:
        endpoint = data.get("telemetry_endpoint")
        if endpoint == LEGACY_FIREBASE_ENDPOINT and merged.get("telemetry_send_policy") != "always":
            merged["telemetry_endpoint"] = None
            merged["telemetry_backend"] = "local"
        elif isinstance(endpoint, str) and "firebaseio.com" in endpoint:
            merged["telemetry_backend"] = "firebase_legacy"
        elif endpoint:
            merged["telemetry_backend"] = "self_hosted"
    return merged


def load_config() -> dict[str, Any]:
    ensure_omm_home()
    if not CONFIG_PATH.exists():
        fresh = {**DEFAULT_CONFIG, "onboarding_completed": False}
        save_config(fresh)
        return fresh
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
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
                loaded = json.loads(CONFIG_PATH.read_text())
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    backup_corrupt_file(CONFIG_PATH)
            except (OSError, json.JSONDecodeError):
                backup_corrupt_file(CONFIG_PATH)
        current = _merge_config(data)
        current.update(changes)
        atomic_write_text(CONFIG_PATH, json.dumps(current, indent=2) + "\n")
    return current
