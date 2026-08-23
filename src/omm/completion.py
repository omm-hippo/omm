"""Shell tab-completion callbacks for omm's Typer CLI. These must never
make a network call, so `install` completion reads only the already-cached
recommend-model artifact (never a live fetch)."""

from __future__ import annotations

from omm import hub, linker, predictor, registry, session_cache


def complete_install_name(incomplete: str) -> list[str]:
    names = set(hub.CURATED_INDEX.keys())

    artifact = predictor.load_cached_model()
    if artifact:
        names.update(c.get("name") for c in artifact.get("candidates", []) if c.get("name"))

    names.update(session_cache.load_seen())

    return sorted(n for n in names if n.startswith(incomplete))


def complete_remove_filename(incomplete: str) -> list[str]:
    filenames = registry.load_registry().keys()
    return sorted(f for f in filenames if f.startswith(incomplete))


def complete_engine_key(incomplete: str) -> list[str]:
    return sorted(spec.key for spec in linker.ENGINES if spec.key.startswith(incomplete))
