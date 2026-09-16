"""Portable model export and provenance; independent of CLI presentation."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable

from omm import linker, scan_import


def export_model_file(source: Path, destination: Path, filename: str, entry: dict, *,
                      force: bool = False,
                      on_copy: Callable[[Path, Path, int], None] | None = None) -> Path:
    exported = linker.export_file(source, destination, on_copy=on_copy, force=force)
    scan_import.write_manifest(exported, manifest_fields(filename, entry, source))
    return exported


def manifest_fields(filename: str, entry: dict, source: Path) -> dict:
    """Portable subset of a registry entry for the `omm export` sidecar -
    only fields meaningful on a different machine. `linked`/`custom_links`/
    `compatibility` are this machine's local state and don't travel."""
    fields: dict = {"schema_version": 1, "filename": filename, "sha256": entry.get("sha256")}
    for key in ("repo_id", "source", "version", "installed_at", "size_bytes"):
        value = entry.get(key)
        if value is not None:
            fields[key] = value

    from omm.gguf import read_gguf_metadata

    try:
        header = read_gguf_metadata(
            source, {"general.architecture", "general.parameter_count"}
        )
    except (OSError, ValueError, struct.error):
        header = {}
    architecture = header.get("general.architecture")
    if isinstance(architecture, str) and architecture:
        fields["architecture"] = architecture
    parameter_count = header.get("general.parameter_count")
    if isinstance(parameter_count, int):
        fields["parameter_count"] = parameter_count

    return fields
