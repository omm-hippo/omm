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
CONFIG_SCHEMA_VERSION = 1
CURRENT_ONBOARDING_VERSION = 1

DEFAULT_CONFIG: dict[str, Any] = {
    "config_schema_version": CONFIG_SCHEMA_VERSION,
    "onboarding_version": 0,
    "ui_mode": "compact",
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
    "catalog_manifest_url": None,
    "catalog_public_key": None,
    "contribute_always_ack": False,
    "update_channel": "stable",
}


def ensure_omm_home() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _merge_config(data: dict[str, Any], *, existing_config: bool = True) -> dict[str, Any]:
    legacy_config = existing_config and "config_schema_version" not in data
    if "telemetry_send_policy" not in data and "telemetry_opt_in" in data:
        data = {
            **data,
            "telemetry_send_policy": "always" if data["telemetry_opt_in"] else "ask",
        }
    merged = {**DEFAULT_CONFIG, **data}
    merged.pop("telemetry_opt_in", None)
    if legacy_config:
        # A config that predates onboarding belongs to an existing user. Do
        # not interrupt their next command with a first-run wizard merely
        # because the new key did not exist in an older release.
        merged["onboarding_version"] = CURRENT_ONBOARDING_VERSION
    merged["config_schema_version"] = CONFIG_SCHEMA_VERSION

    onboarding_version = merged.get("onboarding_version")
    if (
        isinstance(onboarding_version, bool)
        or not isinstance(onboarding_version, int)
        or onboarding_version < 0
    ):
        merged["onboarding_version"] = (
            CURRENT_ONBOARDING_VERSION if existing_config else 0
        )
    if merged.get("ui_mode") not in {"compact", "guided"}:
        merged["ui_mode"] = "compact"
    if merged.get("default_engine") not in {None, "ollama", "lmstudio"}:
        merged["default_engine"] = None
    if merged.get("telemetry_send_policy") not in {"ask", "never", "always"}:
        merged["telemetry_send_policy"] = "ask"
    if merged.get("update_channel") not in {"stable", "beta"}:
        merged["update_channel"] = "stable"

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
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        backup_corrupt_file(CONFIG_PATH)
        return _merge_config({}, existing_config=True)
    if not isinstance(data, dict):
        backup_corrupt_file(CONFIG_PATH)
        return _merge_config({}, existing_config=True)
    return _merge_config(data, existing_config=True)


def save_config(config: dict[str, Any]) -> None:
    ensure_omm_home()
    with locked(CONFIG_PATH):
        atomic_write_text(CONFIG_PATH, json.dumps(config, indent=2) + "\n")


def update_config(**changes: Any) -> dict[str, Any]:
    """Merge a small update while serializing the complete read/write cycle."""
    ensure_omm_home()
    with locked(CONFIG_PATH):
        existing_config = CONFIG_PATH.exists()
        data: dict[str, Any] = {}
        if existing_config:
            try:
                loaded = json.loads(CONFIG_PATH.read_text())
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    backup_corrupt_file(CONFIG_PATH)
            except (OSError, json.JSONDecodeError):
                backup_corrupt_file(CONFIG_PATH)
        current = _merge_config(data, existing_config=existing_config)
        current.update(changes)
        atomic_write_text(CONFIG_PATH, json.dumps(current, indent=2) + "\n")
    return current
