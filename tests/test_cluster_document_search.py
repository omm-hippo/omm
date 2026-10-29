"""How `omm cluster status` tells Ollama's state for document search (#534)."""

from __future__ import annotations

import pytest

from omm import document_embeddings, linker
from omm.cluster import document_search


class _Embedder:
    result: object = True

    def __init__(self, model, base_url=document_embeddings.DEFAULT_URL):
        self.model = model

    def available(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def embedder(monkeypatch):
    monkeypatch.setattr(document_embeddings, "OllamaEmbedder", _Embedder)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    return _Embedder


@pytest.mark.parametrize("pulled", [True, False])
def test_a_reachable_ollama_reports_whether_the_model_is_pulled(embedder, pulled):
    embedder.result = pulled
    assert document_search._ollama("bge-m3", document_embeddings.DEFAULT_URL) == ("running", pulled)


def test_an_unreadable_model_list_is_running_with_unknown_model(embedder):
    embedder.result = document_embeddings.EmbeddingUnavailable("failed", "x")
    assert document_search._ollama("bge-m3", document_embeddings.DEFAULT_URL) == ("running", None)


@pytest.mark.parametrize("installed, state", [(True, "stopped"), (False, "not_installed")])
def test_an_unreachable_ollama_is_stopped_or_not_installed(embedder, monkeypatch, installed, state):
    embedder.result = document_embeddings.EmbeddingUnavailable("server_unavailable", "x")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: installed)
    assert document_search._ollama("bge-m3", document_embeddings.DEFAULT_URL) == (state, None)


def test_a_closed_loopback_port_reads_as_not_running(monkeypatch):
    # The real embedder and HTTP client: nothing listens on port 1.
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    assert document_search._ollama("bge-m3", "http://127.0.0.1:1") == ("stopped", None)


def test_a_head_without_a_document_store_uses_the_defaults_and_creates_nothing(isolated_omm_home):
    from omm import config

    assert document_search._store() == ({"model": "bge-m3", "base_url": "http://127.0.0.1:11434"}, False)
    assert not (config.OMM_HOME / "documents").exists()
