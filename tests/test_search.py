import requests

from omm import search as search_mod


class _Resp:
    """Shared fake-response fixture for tests that just need `.json()` to
    return a fixed payload (the file's other tests define this class inline
    per-test when they need custom `raise_for_status`/`json` behavior; this
    module-level version is for the common "just return a payload" case)."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_guess_family_tinyllama():
    assert search_mod.guess_family("tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf") == "TinyLlama"


def test_guess_family_llama_not_confused_by_tinyllama_substring():
    assert search_mod.guess_family("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") == "Llama"


def test_guess_family_mistral():
    assert search_mod.guess_family("mistral-7b-instruct-v0.2.Q4_K_M.gguf") == "Mistral"


def test_guess_family_other_for_unknown_name():
    assert search_mod.guess_family("some-random-model-name") == "Other"


def test_local_candidate_pool_merges_curated_and_cached_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        search_mod.predictor,
        "load_model",
        lambda url: {
            "candidates": [
                {
                    "name": "tinyllama-1.1b-q4",
                    "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                    "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                    "description": "Curated default",
                },
                {
                    "name": "qwen2.5-7b-instruct-q4",
                    "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                    "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                    "description": "Solid 7B",
                },
            ]
        },
    )

    pool = search_mod.local_candidate_pool(None)

    repo_ids = [c["repo_id"] for c in pool]
    assert repo_ids.count("TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF") == 1
    assert "Qwen/Qwen2.5-7B-Instruct-GGUF" in repo_ids
    # 3 curated (tinyllama/llama3.1/mistral) + 1 new qwen from the cache = 4
    assert len(pool) == 4


def test_search_huggingface_returns_empty_list_on_request_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", _raise)

    assert search_mod.search_huggingface("qwen") == []


def test_search_huggingface_filters_out_fake_provenance_repos(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {
                    "id": "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF",
                    "siblings": [{"rfilename": "model-Q4_K_M.gguf"}],
                },
                {
                    "id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
                    "siblings": [{"rfilename": "mistral-7b-instruct-v0.2.Q4_K_M.gguf"}],
                },
            ]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    results = search_mod.search_huggingface("mistral")

    repo_ids = [c["repo_id"] for c in results]
    assert "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF" not in repo_ids
    assert "TheBloke/Mistral-7B-Instruct-v0.2-GGUF" in repo_ids


def test_claims_fake_provenance_detects_closed_model_brand_names():
    assert search_mod._claims_fake_provenance(
        "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF"
    )
    assert search_mod._claims_fake_provenance("some-model-gpt-4-distill-GGUF")
    assert not search_mod._claims_fake_provenance("TheBloke/Mistral-7B-Instruct-v0.2-GGUF")


def test_search_huggingface_picks_a_concrete_filename_for_multi_quant_repos(monkeypatch):
    # Regression: repos returned by HF search always have several quants
    # (Q3/Q4/Q6/Q8/...). Leaving filename unset meant the printed 'org/repo'
    # string failed `omm install` with "has multiple .gguf files, specify one".
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {
                    "id": "lmstudio-community/granite-3.1-1b-a400m-instruct-GGUF",
                    "siblings": [
                        {"rfilename": "granite-3.1-1b-a400m-instruct-Q3_K_L.gguf"},
                        {"rfilename": "granite-3.1-1b-a400m-instruct-Q4_K_M.gguf"},
                        {"rfilename": "granite-3.1-1b-a400m-instruct-Q6_K.gguf"},
                        {"rfilename": "granite-3.1-1b-a400m-instruct-Q8_0.gguf"},
                    ],
                },
            ]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    results = search_mod.search_huggingface("granite")

    assert results[0]["filename"] == "granite-3.1-1b-a400m-instruct-Q4_K_M.gguf"


def test_search_huggingface_skips_repos_with_no_matching_gguf_file(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": "some-org/no-gguf-here", "siblings": [{"rfilename": "README.md"}]}]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    assert search_mod.search_huggingface("something") == []


def test_pick_gguf_file_prefers_q4_k_m_and_skips_shards():
    siblings = [
        {"rfilename": "model-00001-of-00002.gguf"},
        {"rfilename": "model-Q3_K_L.gguf"},
        {"rfilename": "model-Q4_K_M.gguf"},
        {"rfilename": "model-Q8_0.gguf"},
    ]

    assert search_mod.pick_gguf_file(siblings) == "model-Q4_K_M.gguf"


def test_pick_gguf_file_falls_back_to_first_when_no_preferred_quant():
    siblings = [{"rfilename": "model-Q3_K_L.gguf"}, {"rfilename": "model-Q8_0.gguf"}]

    assert search_mod.pick_gguf_file(siblings) == "model-Q3_K_L.gguf"


def test_pick_gguf_file_returns_none_when_no_gguf_files():
    assert search_mod.pick_gguf_file([{"rfilename": "README.md"}]) is None


def test_install_ref_uses_short_name_for_curated_models():
    candidate = {
        "name": "tinyllama-1.1b-q4",
        "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    }

    assert search_mod.install_ref(candidate) == "tinyllama-1.1b-q4"


def test_install_ref_uses_bare_repo_id_for_non_curated_candidates():
    # Regression: cached-candidate "name" is a sanitized ollama tag
    # (e.g. from scripts/fetch_candidates.py), never a valid
    # `omm install` argument on its own - it has no "/" and isn't a
    # curated key, so resolve_model() rejects it outright. And the
    # filename is dropped: repos ship several quants under one name,
    # `omm install org/repo` already prompts for a quant, so printing a
    # hardcoded filename just makes one repo look like several results.
    candidate = {
        "name": "ibm-granite-granite-4-1-3b-gguf",
        "repo_id": "ibm-granite/granite-4.1-3b-GGUF",
        "filename": "granite-4.1-3b-Q4_K_M.gguf",
    }

    assert search_mod.install_ref(candidate) == "ibm-granite/granite-4.1-3b-GGUF"


def test_install_ref_falls_back_to_repo_id_when_filename_missing():
    assert search_mod.install_ref({"name": "x", "repo_id": "org/repo"}) == "org/repo"


def test_install_ref_prefixes_modelscope_candidates():
    candidate = {"name": "org/repo", "repo_id": "org/repo", "provider": "modelscope"}
    assert search_mod.install_ref(candidate) == "ms:org/repo"


def test_install_ref_leaves_huggingface_candidates_unprefixed():
    candidate = {"name": "org/repo", "repo_id": "org/repo", "provider": "huggingface"}
    assert search_mod.install_ref(candidate) == "org/repo"


def test_install_ref_leaves_curated_names_unprefixed():
    candidate = {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/x", "provider": "huggingface"}
    assert search_mod.install_ref(candidate) == "tinyllama-1.1b-q4"


def test_match_candidates_prefers_substring_match():
    pool = [
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
        {"name": "llama3.1-8b-instruct-q4", "repo_id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"},
    ]

    result = search_mod.match_candidates(pool, "mistral")

    assert [c["name"] for c in result] == ["mistral-7b-instruct-q4"]


def test_match_candidates_falls_back_to_fuzzy_match_on_typo():
    pool = [{"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"}]

    result = search_mod.match_candidates(pool, "mistrall")

    assert result == pool


def test_suggest_similar_limits_and_orders_by_closeness():
    pool = [
        {"name": "tinyllama-1.1b-q4"},
        {"name": "llama3.1-8b-instruct-q4"},
        {"name": "mistral-7b-instruct-q4"},
    ]

    suggestions = search_mod.suggest_similar("tinylama-1.1b-q4", pool, limit=2)

    assert len(suggestions) <= 2
    assert suggestions[0]["name"] == "tinyllama-1.1b-q4"


def test_group_by_family_buckets_by_parsed_family():
    pool = [
        {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"},
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
    ]

    groups = search_mod.group_by_family(pool)

    assert set(groups.keys()) == {"TinyLlama", "Mistral"}
    assert groups["TinyLlama"][0]["name"] == "tinyllama-1.1b-q4"


_MS_SEARCH_PAYLOAD = {
    "success": True,
    "data": {
        "models": [
            {
                "id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                "downloads": 50622,
                "tags": ["library:gguf", "task:text-generation"],
            },
            {
                "id": "some-org/not-gguf-model",
                "downloads": 10,
                "tags": ["task:text-generation"],
            },
        ]
    },
}


def test_search_modelscope_filters_to_gguf_tagged_repos_and_picks_a_file(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _Resp(_MS_SEARCH_PAYLOAD)
    )
    monkeypatch.setattr(
        search_mod.modelscope,
        "fetch_repo_files",
        lambda repo_id, **kwargs: (["qwen2.5-0.5b-instruct-q4_k_m.gguf"], None),
    )
    results = search_mod.search_modelscope("qwen2.5")
    assert len(results) == 1
    assert results[0]["repo_id"] == "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
    assert results[0]["filename"] == "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert results[0]["provider"] == "modelscope"


def test_search_modelscope_skips_fake_provenance_repos(monkeypatch):
    payload = {
        "success": True,
        "data": {
            "models": [
                {"id": "someone/claude-4-opus-gguf", "downloads": 1, "tags": ["library:gguf"]},
                {
                    "id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                    "downloads": 50622,
                    "tags": ["library:gguf"],
                },
            ]
        },
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(
        search_mod.modelscope,
        "fetch_repo_files",
        lambda repo_id, **kwargs: ([f"{repo_id.split('/')[-1]}-Q4_K_M.gguf"], None),
    )

    results = search_mod.search_modelscope("claude")

    repo_ids = [c["repo_id"] for c in results]
    assert "someone/claude-4-opus-gguf" not in repo_ids
    assert "Qwen/Qwen2.5-0.5B-Instruct-GGUF" in repo_ids


def test_search_modelscope_returns_empty_list_on_malformed_top_level_response(monkeypatch):
    # Regression: {"success": True, "data": None} is a plausible
    # empty/error ModelScope response. `.get("data", {})` doesn't guard
    # against an explicit `None` value, so `.get("models", [])` on it used
    # to crash with AttributeError instead of degrading gracefully.
    payload = {"success": True, "data": None}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))

    assert search_mod.search_modelscope("query") == []


def test_search_modelscope_skips_single_failing_repo_and_keeps_others(monkeypatch):
    payload = {
        "success": True,
        "data": {
            "models": [
                {"id": "org/broken-repo-GGUF", "downloads": 5, "tags": ["library:gguf"]},
                {
                    "id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                    "downloads": 50622,
                    "tags": ["library:gguf"],
                },
            ]
        },
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))

    def _fake_fetch_repo_files(repo_id, **kwargs):
        if repo_id == "org/broken-repo-GGUF":
            raise requests.RequestException("boom")
        return ["qwen2.5-0.5b-instruct-q4_k_m.gguf"], None

    monkeypatch.setattr(search_mod.modelscope, "fetch_repo_files", _fake_fetch_repo_files)

    results = search_mod.search_modelscope("qwen2.5")

    repo_ids = [c["repo_id"] for c in results]
    assert "org/broken-repo-GGUF" not in repo_ids
    assert "Qwen/Qwen2.5-0.5B-Instruct-GGUF" in repo_ids


def test_search_huggingface_results_are_tagged_huggingface(monkeypatch):
    payload = [
        {
            "id": "org/repo",
            "siblings": [{"rfilename": "model.Q4_K_M.gguf"}],
        }
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    results = search_mod.search_huggingface("query")
    assert results[0]["provider"] == "huggingface"
