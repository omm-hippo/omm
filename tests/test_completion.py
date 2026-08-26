from omm import completion


def test_complete_install_name_includes_curated_names(monkeypatch):
    monkeypatch.setattr(completion.predictor, "load_cached_model", lambda: None)

    result = completion.complete_install_name("tiny")

    assert "tinyllama-1.1b-q4" in result


def test_complete_install_name_includes_cached_candidate_names(monkeypatch):
    monkeypatch.setattr(
        completion.predictor,
        "load_cached_model",
        lambda: {"candidates": [{"name": "qwen2.5-7b-instruct-q4"}]},
    )

    result = completion.complete_install_name("qwen")

    assert result == ["qwen2.5-7b-instruct-q4"]


def test_complete_install_name_ignores_malformed_cached_candidates(monkeypatch):
    monkeypatch.setattr(
        completion.predictor,
        "load_cached_model",
        lambda: {"candidates": [None, "bad", {}, {"name": "qwen-safe"}]},
    )

    assert completion.complete_install_name("qwen") == ["qwen-safe"]


def test_complete_install_name_filters_by_prefix(monkeypatch):
    monkeypatch.setattr(completion.predictor, "load_cached_model", lambda: None)

    result = completion.complete_install_name("mistral")

    assert result == ["mistral-7b-instruct-q4"]


def test_complete_install_name_includes_session_seen_names(monkeypatch):
    monkeypatch.setattr(completion.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        completion.session_cache,
        "load_seen",
        lambda: ["org/repo:some-file-Q4_K_M.gguf"],
    )

    result = completion.complete_install_name("org/repo")

    assert result == ["org/repo:some-file-Q4_K_M.gguf"]


def test_complete_remove_filename_reads_registry_and_filters(monkeypatch):
    monkeypatch.setattr(
        completion.registry,
        "load_registry",
        lambda: {
            "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf": {},
            "mistral-7b-instruct-v0.2.Q4_K_M.gguf": {},
        },
    )

    result = completion.complete_remove_filename("tiny")

    assert result == ["tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"]
