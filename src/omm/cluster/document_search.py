"""Whether the head's document search (RAG) is semantic or keyword-only (#534).

Semantic search needs a loopback Ollama with the embedding model set by
`omm rag embedding-model`. Without it, uploads and searches fall back to
keyword matching, and a head that was started by hand loses Ollama after a
reboot. `omm cluster status` only reports this and the fix: nothing here
installs, starts or pulls anything.
"""

from __future__ import annotations

import sqlite3

SEMANTIC = "semantic"
KEYWORD = "keyword"


def _store() -> tuple[dict, bool]:
    """(embedding settings, whether the head has documents or its own collections).

    Never creates the document store: a head without one gets the defaults.
    """
    from omm import config, document_embeddings

    defaults = {"model": document_embeddings.DEFAULT_MODEL, "base_url": document_embeddings.DEFAULT_URL}
    if not (config.OMM_HOME / "documents" / "library.sqlite3").is_file():
        return defaults, False
    from omm.documents import DEFAULT_COLLECTION, DocumentStore

    try:
        store = DocumentStore()
        found = store.embedding_settings()
        collections = store.collections()
    except (OSError, ValueError, sqlite3.Error):
        # Unreadable store: report a missing Ollama rather than hide it.
        return defaults, True
    in_use = any(row["documents"] or row["id"] != DEFAULT_COLLECTION for row in collections)
    return {"model": found["model"], "base_url": found["base_url"]}, in_use


def _ollama(model: str, base_url: str) -> tuple[str, bool | None]:
    """("running" | "stopped" | "not_installed", whether `model` is pulled; None if unknown)."""
    from omm import document_embeddings

    try:
        return "running", document_embeddings.OllamaEmbedder(model, base_url).available()
    except document_embeddings.EmbeddingUnavailable as error:
        if error.reason != "server_unavailable":
            return "running", None
    except ValueError:
        pass  # not a loopback URL; the embedder would refuse it too
    from omm import linker

    return ("stopped" if linker.is_ollama_installed() else "not_installed"), None


def _start_command() -> str:
    """How to start Ollama so it also comes back after a reboot or logout."""
    import os
    import shutil

    from omm import linker

    binary = shutil.which("ollama")
    if linker.engine_app_bundle_path("ollama") is not None and (
            binary is None or ".app/" in os.path.realpath(binary)):
        return ("open -a Ollama (keep Ollama in System Settings > General > Login Items "
                "so it starts again after a reboot)")
    return "brew services start ollama (it then starts again after a reboot or logout)"


def check() -> tuple[dict, list[str]]:
    """The head's document-search readiness and its problems (none while no documents exist)."""
    settings, in_use = _store()
    model, url = settings["model"], settings["base_url"]
    ollama, pulled = _ollama(model, url)
    mode = SEMANTIC if ollama == "running" and pulled else KEYWORD
    report = {"mode": mode, "model": model, "url": url, "ollama": ollama, "model_pulled": pulled,
              "in_use": in_use}
    if mode == SEMANTIC or not in_use:
        return report, []
    prefix = "document search is keyword-only"
    if ollama == "not_installed":
        problem = (f"{prefix}: Ollama is not installed; run brew install ollama, then "
                   f"brew services start ollama (it then starts again after a reboot or logout), "
                   f"then ollama pull {model}")
    elif ollama == "stopped":
        problem = f"{prefix}: Ollama is not running at {url}; run {_start_command()}"
    elif pulled is None:
        problem = f"{prefix}: Ollama at {url} did not list its models; restart Ollama and check again"
    else:
        problem = f"{prefix}: Ollama does not have the embedding model {model}; run ollama pull {model}"
    return report, [problem]


def summary(report: dict) -> str:
    """One status line, e.g. "semantic (Ollama, bge-m3)"."""
    if report["mode"] == SEMANTIC:
        reason = f"Ollama, {report['model']}"
    elif report["ollama"] == "not_installed":
        reason = "Ollama not installed"
    elif report["ollama"] == "stopped":
        reason = "Ollama not running"
    elif report["model_pulled"] is None:
        reason = "Ollama model list unreadable"
    else:
        reason = f"{report['model']} not pulled in Ollama"
    if not report["in_use"]:
        reason += "; no documents yet"
    return f"{'semantic' if report['mode'] == SEMANTIC else 'keyword only'} ({reason})"
