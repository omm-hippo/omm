"""Tests for scripts/fetch_candidates.py's ModelScope fetch + cross-provider
dedupe logic. Doesn't test fetch_trending_candidates (HF) since that's
unchanged from before this feature and already implicitly covered by
test_search.py's HF-fetching tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_candidates  # noqa: E402


def test_fetch_modelscope_candidates_tags_provider(monkeypatch):
    # fetch_modelscope_candidates() issues one search_modelscope() call per
    # entry in MODELSCOPE_QUERIES and doesn't dedupe internally (that's
    # main()'s job - see test_main_dedupes_by_provider_and_repo_id below), so
    # the fake mirrors a real search API: different queries return different
    # results. Only the first query yields a hit here.
    queries_seen: list[str] = []

    def fake_search_modelscope(query, **kwargs):
        queries_seen.append(query)
        if query != fetch_candidates.MODELSCOPE_QUERIES[0]:
            return []
        return [
            {
                "name": "org/repo",
                "repo_id": "org/repo",
                "filename": "model.gguf",
                "description": "ModelScope",
                "provider": "modelscope",
            }
        ]

    monkeypatch.setattr(
        fetch_candidates.search_mod, "search_modelscope", fake_search_modelscope
    )
    candidates = fetch_candidates.fetch_modelscope_candidates()
    assert candidates == [
        {
            "name": "org/repo",
            "repo_id": "org/repo",
            "filename": "model.gguf",
            "description": "ModelScope",
            "provider": "modelscope",
        }
    ]
    assert queries_seen == fetch_candidates.MODELSCOPE_QUERIES


def test_main_dedupes_by_provider_and_repo_id(monkeypatch, tmp_path):
    hf_candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "a.gguf",
        "description": "HF",
        "provider": "huggingface",
    }
    ms_candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "b.gguf",
        "description": "MS",
        "provider": "modelscope",
    }
    monkeypatch.setattr(fetch_candidates, "curated_candidates", lambda: [])
    monkeypatch.setattr(fetch_candidates, "fetch_trending_candidates", lambda: [hf_candidate])
    monkeypatch.setattr(
        fetch_candidates, "fetch_modelscope_candidates", lambda: [ms_candidate]
    )
    output_path = tmp_path / "candidates.json"
    monkeypatch.setattr(fetch_candidates, "OUTPUT_PATH", output_path)

    fetch_candidates.main()

    import json

    written = json.loads(output_path.read_text())
    # Same repo_id, different provider - both must survive the dedupe.
    assert len(written) == 2
    assert {c["provider"] for c in written} == {"huggingface", "modelscope"}
