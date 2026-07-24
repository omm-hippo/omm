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


def test_info_shows_name_version_size_and_links(isolated_omm_home):
    registry.save_registry({"model.gguf": _entry()})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout
    assert "abc1234" in result.stdout
    assert "2.00 GB" in result.stdout
    assert "ollama run repo-q4" in result.stdout
    assert "LM Studio" in result.stdout


def test_info_falls_back_to_sha256_prefix_when_version_missing(isolated_omm_home):
    entry = _entry()
    del entry["version"]
    registry.save_registry({"model.gguf": entry})

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "abc1234" in result.stdout


def test_info_shows_not_linked_for_unlinked_engines(isolated_omm_home):
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


def test_info_errors_for_uninstalled_model(isolated_omm_home):
    result = runner.invoke(cli.app, ["info", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_info_accepts_numeric_index_from_last_results(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": _entry()})
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: ["model.gguf"])

    result = runner.invoke(cli.app, ["info", "1"])

    assert result.exit_code == 0, result.stdout
    assert "model.gguf" in result.stdout
