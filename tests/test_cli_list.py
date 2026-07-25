import json

from typer.testing import CliRunner

from omm import cli, linker, registry

runner = CliRunner()


def _all_linked(**overrides) -> dict:
    linked = {spec.key: False for spec in linker.ENGINES}
    linked.update(overrides)
    return linked


def test_list_shows_index_column_and_records_session(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "a.gguf": {"size_bytes": 0, "linked": {"lmstudio": False, "ollama": False}},
            "b.gguf": {"size_bytes": 0, "linked": {"lmstudio": False, "ollama": True}},
        }
    )
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert recorded == [["a.gguf", "b.gguf"]]


def test_list_json_is_parseable_and_has_expected_fields(isolated_omm_home):
    registry.save_registry(
        {
            "a.gguf": {"size_bytes": 5, "linked": {"lmstudio": False, "ollama": False}},
            "b.gguf": {"size_bytes": 9, "linked": {"lmstudio": False, "ollama": True}},
        }
    )

    result = runner.invoke(cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data == [
        {"index": 1, "filename": "a.gguf", "size_bytes": 5, "linked": _all_linked()},
        {"index": 2, "filename": "b.gguf", "size_bytes": 9, "linked": _all_linked(ollama=True)},
    ]


def test_list_json_empty_registry_prints_empty_array(isolated_omm_home):
    result = runner.invoke(cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == []


def test_list_empty_registry_does_not_touch_session(isolated_omm_home, monkeypatch):
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert recorded == []
