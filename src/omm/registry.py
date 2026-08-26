"""Central registry of models installed via omm (~/.omm/models.json)."""

from __future__ import annotations

import json
from typing import Any

from omm.atomic import atomic_write_text, backup_corrupt_file, locked
from omm.config import REGISTRY_PATH, ensure_omm_home


def load_registry() -> dict[str, Any]:
    ensure_omm_home()
    if not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup_corrupt_file(REGISTRY_PATH)
        return {}
    if not isinstance(data, dict):
        backup_corrupt_file(REGISTRY_PATH)
        return {}
    return data


def save_registry(registry: dict[str, Any]) -> None:
    ensure_omm_home()
    with locked(REGISTRY_PATH):
        _save_registry_unlocked(registry)


def _save_registry_unlocked(registry: dict[str, Any]) -> None:
    atomic_write_text(REGISTRY_PATH, json.dumps(registry, indent=2) + "\n")


def upsert_entry(filename: str, **fields: Any) -> None:
    ensure_omm_home()
    with locked(REGISTRY_PATH):
        registry = load_registry()
        entry = registry.get(filename)
        if not isinstance(entry, dict):
            entry = {"linked": {}}
            registry[filename] = entry
        entry.update({k: v for k, v in fields.items() if k != "linked"})
        if "linked" in fields:
            linked = entry.get("linked")
            if not isinstance(linked, dict):
                linked = {}
                entry["linked"] = linked
            incoming = fields["linked"]
            if not isinstance(incoming, dict):
                raise TypeError("linked registry metadata must be a dictionary")
            linked.update(incoming)
        _save_registry_unlocked(registry)


def record_compatibility(filename: str, engine: str, result: dict[str, Any]) -> None:
    """Atomically store one engine result without replacing sibling engines."""
    ensure_omm_home()
    with locked(REGISTRY_PATH):
        registry = load_registry()
        entry = registry.get(filename)
        if not isinstance(entry, dict):
            raise KeyError(filename)
        compatibility = entry.get("compatibility")
        if not isinstance(compatibility, dict):
            compatibility = {}
            entry["compatibility"] = compatibility
        compatibility[engine] = dict(result)
        _save_registry_unlocked(registry)


def remove_entry(filename: str) -> None:
    ensure_omm_home()
    with locked(REGISTRY_PATH):
        registry = load_registry()
        registry.pop(filename, None)
        _save_registry_unlocked(registry)
