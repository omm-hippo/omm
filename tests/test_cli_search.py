import json

from typer.testing import CliRunner

from omm import cli, search as search_mod

runner = CliRunner()


def test_search_groups_results_by_family(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        search_mod,
        "local_candidate_pool",
        lambda model_url: [
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
        lambda model_url: [
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


def test_search_json_is_parseable_and_has_expected_fields(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [
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


def test_search_command_includes_modelscope_results(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url: [])
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
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "nonexistent-xyz"])

    assert result.exit_code == 1
    assert "No models found" in result.stderr
