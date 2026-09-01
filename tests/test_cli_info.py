import json

from typer.testing import CliRunner

from omm import cli, linker, registry

runner = CliRunner()


def _all_linked(**overrides) -> dict:
    linked = {spec.key: False for spec in linker.ENGINES}
    linked.update(overrides)
    return linked


def _entry(**overrides):
    entry = {
        "sha256": "abc1234567890",
        "version": "abc1234",
        "size_bytes": 2 * 1024**3,
        "installed_at": "2026-07-19T00:00:00+00:00",
        "repo_id": "org/repo",
        "ollama_name": "repo-q4",
        "linked": {"lmstudio": True, "ollama": True},
    }
    entry.update(overrides)
    return entry


def test_info_shows_name_version_size_and_links(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in ("ollama", "lmstudio"))
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout
    assert "abc1234" in result.stdout
    assert "2.00 GB" in result.stdout
    assert "ollama run repo-q4" in result.stdout
    assert "LM Studio" in result.stdout


def test_info_falls_back_to_sha256_prefix_when_version_missing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)
    entry = _entry()
    del entry["version"]
    registry.save_registry({"model.gguf": entry})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "abc1234" in result.stdout


def test_info_shows_not_linked_for_unlinked_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in ("ollama", "lmstudio"))
    entry = _entry(linked={"lmstudio": False, "ollama": False})
    registry.save_registry({"model.gguf": entry})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "not linked" in result.stdout


def test_info_json_is_parseable_and_has_expected_fields(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["filename"] == "model.gguf"
    assert data["version"] == "abc1234"
    assert data["size_bytes"] == 2 * 1024**3
    assert data["linked"] == _all_linked(lmstudio=True, ollama=True)
    assert data["ollama_run_command"] == "ollama run repo-q4"


def test_info_json_before_subcommand(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["--json", "info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["filename"] == "model.gguf"
    assert data["version"] == "abc1234"
    assert data["size_bytes"] == 2 * 1024**3
    assert data["linked"] == _all_linked(lmstudio=True, ollama=True)
    assert data["ollama_run_command"] == "ollama run repo-q4"


def test_info_errors_for_uninstalled_model(isolated_omm_home):
    result = runner.invoke(cli.app, ["info", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr
    assert "→ Run `omm list` to see what is installed." in result.stderr


def test_info_accepts_numeric_index_from_last_results(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: ["model.gguf"])
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)

    result = runner.invoke(cli.app, ["info", "1"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout


def test_info_hides_rows_for_uninstalled_engines(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "Ollama" in result.stdout
    assert "LM Studio" not in result.stdout
    assert "Jan" not in result.stdout


def test_info_notes_missing_engine_count_with_wiki_link(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key == "ollama")
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    missing_count = len(linker.ENGINES) - 1
    assert f"+ {missing_count} program(s) not installed" in result.stdout
    assert cli.COMPATIBLE_PROGRAMS_URL in result.stdout


def test_info_omits_missing_note_when_all_engines_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: True)
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "not installed" not in result.stdout


def _resolved(**overrides):
    resolved = cli.ResolvedModel(
        url="https://huggingface.co/org/repo/resolve/main/model-Q4_K_M.gguf",
        filename="model-Q4_K_M.gguf",
        repo_id="org/repo",
        provider="huggingface",
    )
    for key, value in overrides.items():
        setattr(resolved, key, value)
    return resolved


def _stub_remote(monkeypatch, *, resolved=None, size_bytes=4 * 1024**3, metadata=None):
    monkeypatch.setattr(cli, "_resolve_model_interactive", lambda name: resolved or _resolved())
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: size_bytes)
    monkeypatch.setattr(
        cli,
        "fetch_repo_metadata",
        lambda provider, repo_id: metadata
        if metadata is not None
        else {
            "author": "org",
            "downloads": 319887,
            "likes": 74,
            "license": "apache-2.0",
            "architecture": "qwen2",
            "context_length": 32768,
            "last_modified": "2024-09-19T12:54:25.000Z",
            "url": "https://huggingface.co/org/repo",
        },
    )


def test_info_resolves_a_model_that_is_not_installed(isolated_omm_home, monkeypatch):
    _stub_remote(monkeypatch)

    result = runner.invoke(cli.app, ["info", "org/repo"])

    assert result.exit_code == 0, result.stdout
    assert "model-Q4_K_M.gguf" in result.stdout
    assert "not installed" in result.stdout
    assert "4.00 GB" in result.stdout


def test_info_shows_provider_metadata_for_a_search_result(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: ["org/repo"])
    _stub_remote(monkeypatch)

    result = runner.invoke(cli.app, ["info", "1"])

    assert result.exit_code == 0, result.stdout
    assert "319,887" in result.stdout  # downloads, grouped
    assert "apache-2.0" in result.stdout
    assert "qwen2" in result.stdout
    assert "32,768 tokens" in result.stdout
    assert "2024-09-19" in result.stdout
    assert "12:54:25" not in result.stdout  # the timestamp is trimmed to a date
    assert "https://huggingface.co/org/repo" in result.stdout


def test_info_omits_rows_the_provider_did_not_report(isolated_omm_home, monkeypatch):
    _stub_remote(monkeypatch, metadata={"author": "org"})

    result = runner.invoke(cli.app, ["info", "org/repo"])

    assert result.exit_code == 0, result.stdout
    assert "Author" in result.stdout
    assert "License" not in result.stdout
    assert "Context length" not in result.stdout


def test_info_points_at_install_and_fit_for_an_uninstalled_model(isolated_omm_home, monkeypatch):
    _stub_remote(monkeypatch)

    result = runner.invoke(cli.app, ["info", "org/repo"])

    assert "omm install org/repo:model-Q4_K_M.gguf" in result.stdout
    assert "omm fit org/repo:model-Q4_K_M.gguf" in result.stdout


def test_info_json_for_an_uninstalled_model(isolated_omm_home, monkeypatch):
    _stub_remote(monkeypatch)

    result = runner.invoke(cli.app, ["info", "org/repo", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["installed"] is False
    assert payload["filename"] == "model-Q4_K_M.gguf"
    assert payload["repo_id"] == "org/repo"
    assert payload["provider"] == "huggingface"
    assert payload["size_bytes"] == 4 * 1024**3
    assert payload["downloads"] == 319887
    assert payload["license"] == "apache-2.0"


def test_info_reports_a_reference_no_provider_could_resolve(isolated_omm_home, monkeypatch):
    def _explode(name):
        raise cli.ModelResolutionError(f"HF repo '{name}' not found.")

    monkeypatch.setattr(cli, "_resolve_model_interactive", _explode)

    result = runner.invoke(cli.app, ["info", "org/nope"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr
    assert "not found" in result.stderr


def test_info_does_not_print_the_fit_card(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "In use by other apps" not in result.stdout
    assert "Safe model budget" not in result.stdout
    assert "omm fit model.gguf" in result.stdout
