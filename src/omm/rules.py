"""Hardware-based model recommendation rules.

Ships with a small bundled default so `omm recommend` works offline. `omm
update` can overwrite ~/.omm/rules.json with a hosted index later.
"""

from __future__ import annotations

import json

from omm.atomic import atomic_write_text, backup_corrupt_file
from omm.config import RULES_PATH

DEFAULT_RULES: list[dict] = [
    {
        "name": "tinyllama-1.1b-q4",
        "min_ram_gb": 2,
        "min_vram_gb": 0,
        "description": "Tiny 1.1B model, runs on almost any machine (CPU only).",
    },
    {
        "name": "mistral-7b-instruct-q4",
        "min_ram_gb": 8,
        "min_vram_gb": 6,
        "description": "Solid general-purpose 7B assistant, Q4_K_M.",
    },
    {
        "name": "llama3.1-8b-instruct-q4",
        "min_ram_gb": 10,
        "min_vram_gb": 8,
        "description": "Meta Llama 3.1 8B Instruct, Q4_K_M.",
    },
]


def _read_rules_file() -> list[dict] | None:
    if not RULES_PATH.exists():
        return None
    try:
        return json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup_corrupt_file(RULES_PATH)
        return None


def load_rules() -> list[dict]:
    rules = _read_rules_file()
    return rules if rules is not None else DEFAULT_RULES


def fetch_rules(url: str) -> list[dict]:
    import requests

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    rules = resp.json()
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(RULES_PATH, json.dumps(rules, indent=2))
    return rules


def refresh_rules_with_change_note(url: str) -> tuple[list[dict], bool]:
    """Like fetch_rules, but also reports whether the fetched rules differ
    from what was already cached."""
    previous = _read_rules_file()
    fetched = fetch_rules(url)
    return fetched, fetched != previous


def matching_rules(rules: list[dict], available_gb: float, has_gpu: bool) -> list[dict]:
    matches = []
    for rule in rules:
        needed = rule["min_vram_gb"] if has_gpu else rule["min_ram_gb"]
        if available_gb >= needed:
            matches.append(rule)
    return sorted(matches, key=lambda r: r["min_ram_gb"], reverse=True)
