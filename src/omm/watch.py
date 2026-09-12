"""Background auto-import: watches every supported local AI app's model
directory and, when a new file settles, runs the same scan -> group -> adopt
pipeline as `omm import` (see scan_import.py) without a prompt.

Adopting a file that is still being written by another process is unsafe -
scan_import.adopt_group re-hashes right before finalizing the move and
raises linker.LinkError if the content changed underneath it, but paying for
a full sha256 of a large in-progress download just to hit that guard is
wasteful. _is_group_stable is a cheap pre-filter: skip anything whose size
is still moving and pick it up again next pass once it has settled.

Design: docs/superpowers/specs/2026-09-12-auto-import-watch-design.md
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from omm import linker, notify, registry, scan_import

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 15.0
STABILITY_WAIT_SECONDS = 2.0


def watch_target_dirs() -> list[Path]:
    """Every real, existing local-app model directory scan_import.py already
    knows how to scan. A missing (uninstalled) app's directory is silently
    skipped - watchdog can't watch a directory that doesn't exist."""
    candidates = [
        linker.ollama_models_dir(),
        linker.lmstudio_models_dir(),
        linker.anythingllm_ollama_models_dir(),
        linker.mstystudio_models_dir(),
        linker.textgenwebui_models_dir(),
        linker.koboldcpp_models_dir(),
        linker.jan_models_dir(),
    ]
    return [path for path in candidates if path is not None and path.exists()]


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _is_group_stable(group: scan_import.ModelGroup) -> bool:
    sizes_before = [_file_size(loc.path) for loc in group.locations]
    if any(size is None for size in sizes_before):
        return False
    time.sleep(STABILITY_WAIT_SECONDS)
    sizes_after = [_file_size(loc.path) for loc in group.locations]
    return sizes_before == sizes_after


def run_once() -> list[scan_import.AdoptResult]:
    """One full scan -> group -> adopt pass. Skips any group whose hash is
    already in the hub and any group that hasn't settled yet. Returns the
    AdoptResult for every model newly adopted this pass (empty if nothing
    new)."""
    found = scan_import.find_external_models()
    groups = scan_import.group_by_hash(found)
    reg = registry.load_registry()
    known_hashes = {
        entry.get("sha256") for entry in reg.values() if isinstance(entry, dict)
    }

    results: list[scan_import.AdoptResult] = []
    for group in groups:
        if group.sha256 in known_hashes:
            continue
        if not _is_group_stable(group):
            continue
        try:
            result = scan_import.adopt_group(group)
        except (OSError, linker.LinkError) as error:
            log.info("Skipping %s this pass: %s", group.display_name, error)
            continue
        results.append(result)
        notify.notify(
            "omm: new model auto-imported",
            f"{result.filename} is now available to every installed local AI app.",
        )
    return results
