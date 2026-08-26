import json

from typer.testing import CliRunner

from omm import cli, search as search_mod

runner = CliRunner()


def test_search_groups_results_by_family(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
            {
                "name": "mistral-7b-instruct-q4",
                "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "q4"])

    assert result.exit_code == 0, result.stdout
    assert "==> TinyLlama" in result.stdout
    assert "==> Mistral" in result.stdout
    assert "tinyllama-1.1b-q4" in result.stdout
    assert "mistral-7b-instruct-q4" in result.stdout


def test_search_prints_numbered_refs_and_records_session(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["search", "tiny"])

    assert result.exit_code == 0, result.stdout
    assert "[1] tinyllama-1.1b-q4" in result.stdout
    assert recorded == [["tinyllama-1.1b-q4"]]


def test_search_prints_install_shortcut_hint(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "tiny"])

    assert result.exit_code == 0, result.stdout
    assert "omm install <number>" in result.stdout
    assert "omm install 1" in result.stdout


def test_search_json_omits_install_shortcut_hint(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["--json", "search", "tiny"])

    assert result.exit_code == 0, result.stdout
    assert "omm install" not in result.stdout


def test_search_json_is_parseable_and_has_expected_fields(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    result = runner.invoke(cli.app, ["search", "tiny", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == [
        {
            "index": 1,
            "family": "TinyLlama",
            "ref": "tinyllama-1.1b-q4",
            "description": "Curated default",
            "fits_hardware": True,
        }
    ]


def test_search_json_before_subcommand(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    result = runner.invoke(cli.app, ["--json", "search", "tiny"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == [
        {
            "index": 1,
            "family": "TinyLlama",
            "ref": "tinyllama-1.1b-q4",
            "description": "Curated default",
            "fits_hardware": True,
        }
    ]


def test_search_command_includes_modelscope_results(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(
        cli.search_mod,
        "search_modelscope",
        lambda query, **kwargs: [
            {
                "name": "org/repo",
                "repo_id": "org/repo",
                "filename": "model.gguf",
                "description": "1,000 downloads on ModelScope",
                "provider": "modelscope",
            }
        ],
    )
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["search", "repo"])

    assert result.exit_code == 0, result.stdout
    assert "[1] ms:org/repo" in result.stdout
    assert recorded == [["ms:org/repo"]]


def test_search_exits_nonzero_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "nonexistent-xyz"])

    assert result.exit_code == 1
    assert "No models found" in result.stderr


def test_search_limit_stops_mid_family_before_later_candidates(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {"name": "llama-7b-model", "repo_id": "org/llama-7b-model", "description": "d"},
            {"name": "llama-13b-model", "repo_id": "org/llama-13b-model", "description": "d"},
            {"name": "llama-30b-model", "repo_id": "org/llama-30b-model", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "model", "--limit", "2"])

    assert result.exit_code == 0, result.stdout
    assert "llama-7b-model" in result.stdout
    assert "llama-13b-model" in result.stdout
    assert "llama-30b-model" not in result.stdout


def test_search_limit_stops_family_headers_once_reached(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {"name": "gemma-model-a", "repo_id": "org/gemma-model-a", "description": "d"},
            {"name": "mistral-model-b", "repo_id": "org/mistral-model-b", "description": "d"},
            {"name": "qwen-model-c", "repo_id": "org/qwen-model-c", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "model", "--limit", "2"])

    assert result.exit_code == 0, result.stdout
    assert "==> Gemma" in result.stdout
    assert "==> Mistral" in result.stdout
    assert "==> Qwen" not in result.stdout
    assert "qwen-model-c" not in result.stdout


def test_search_limit_skips_param_count_network_fallback_past_the_limit(monkeypatch):
    # The per-candidate parameter-count fallback (fetch_repo_param_count_b)
    # is a network call. Once --limit results are already collected, later
    # candidates must never reach it - the limit check has to run before the
    # fallback, not after (see #81).
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {"name": "llama-7b-model", "repo_id": "org/llama-7b-model", "description": "d"},
            {"name": "llama-13b-model", "repo_id": "org/llama-13b-model", "description": "d"},
            {"name": "llama-30b-model", "repo_id": "org/llama-30b-model", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: 1.0)
    monkeypatch.setattr(cli, "candidate_parameter_count_billions", lambda c: None)
    fallback_calls = []
    monkeypatch.setattr(
        cli,
        "fetch_repo_param_count_b",
        lambda provider, repo_id: fallback_calls.append(repo_id) or 7.0,
    )

    result = runner.invoke(cli.app, ["search", "model", "--limit", "2"])

    assert result.exit_code == 0, result.stdout
    assert fallback_calls == ["org/llama-7b-model", "org/llama-13b-model"]


def test_search_provider_curated_filters_out_remote_results(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {"name": "mistral-curated", "repo_id": "org/mistral-curated", "description": "d"},
        ],
    )
    monkeypatch.setattr(
        cli.search_mod,
        "search_huggingface",
        lambda query, **kwargs: [
            {
                "name": "mistral-hf",
                "repo_id": "org/mistral-hf",
                "filename": "model.gguf",
                "description": "d",
                "provider": "huggingface",
            }
        ],
    )
    monkeypatch.setattr(
        cli.search_mod,
        "search_modelscope",
        lambda query, **kwargs: [
            {
                "name": "mistral-ms",
                "repo_id": "org/mistral-ms",
                "filename": "model.gguf",
                "description": "d",
                "provider": "modelscope",
            }
        ],
    )

    result = runner.invoke(cli.app, ["search", "mistral", "--provider", "curated"])

    assert result.exit_code == 0, result.stdout
    assert "mistral-curated" in result.stdout
    assert "mistral-hf" not in result.stdout
    assert "mistral-ms" not in result.stdout


def test_search_provider_curated_does_not_query_remote_providers(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url, **kwargs: [
            {"name": "mistral-curated", "repo_id": "org/mistral-curated", "description": "d"},
        ],
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("a curated-only search must not query remote providers")

    monkeypatch.setattr(cli.search_mod, "search_huggingface", unexpected)
    monkeypatch.setattr(cli.search_mod, "search_modelscope", unexpected)

    result = runner.invoke(cli.app, ["search", "mistral", "--provider", "curated"])

    assert result.exit_code == 0, result.stdout


def test_search_provider_bogus_value_errors(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "model", "--provider", "bogus"])

    assert result.exit_code == 2
    assert "--provider must be one of" in result.stderr


def test_search_skip_ms_never_calls_modelscope(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(
        cli.search_mod,
        "search_huggingface",
        lambda query, **kwargs: [
            {
                "name": "mistral-hf",
                "repo_id": "org/mistral-hf",
                "filename": "model.gguf",
                "description": "d",
                "provider": "huggingface",
            }
        ],
    )

    def _fail_if_called(query, **kwargs):
        raise AssertionError("search_modelscope should not be called with --skip-ms")

    monkeypatch.setattr(cli.search_mod, "search_modelscope", _fail_if_called)

    result = runner.invoke(cli.app, ["search", "mistral", "--skip-ms"])

    assert result.exit_code == 0, result.stdout
    assert "mistral-hf" in result.stdout


def test_search_skip_ms_conflicts_with_provider_modelscope(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(
        cli.app, ["search", "model", "--skip-ms", "--provider", "modelscope"]
    )

    assert result.exit_code == 2
    assert "--skip-ms conflicts with --provider modelscope" in result.stderr
