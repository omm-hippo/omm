from typer.testing import CliRunner

from omm import cli
from omm.hub import ModelResolutionError

runner = CliRunner()


def test_install_resolves_numeric_arg_from_last_results(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda **_: ["org/repo:file.gguf"])
    monkeypatch.setattr(cli, "_print_install_suggestions", lambda name: None)
    seen = {}

    def fake_resolve_model(name):
        seen["name"] = name
        raise ModelResolutionError("stop here, we only care about the arg")

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)

    runner.invoke(cli.app, ["install", "1"])

    assert seen["name"] == "org/repo:file.gguf"


def test_install_numeric_arg_out_of_range(monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda **_: ["only-one"])

    result = runner.invoke(cli.app, ["install", "5"])

    assert result.exit_code == 1
    assert "5" in result.stderr


def test_install_numeric_arg_with_no_prior_results(monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda **_: [])

    result = runner.invoke(cli.app, ["install", "1"])

    assert result.exit_code == 1
    assert "omm search" in result.stderr or "omm list" in result.stderr


def test_install_non_numeric_arg_is_unaffected(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda **_: ["should-not-be-used"])
    monkeypatch.setattr(cli, "_print_install_suggestions", lambda name: None)
    seen = {}

    def fake_resolve_model(name):
        seen["name"] = name
        raise ModelResolutionError("stop")

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)

    runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert seen["name"] == "tinyllama-1.1b-q4"


def test_remove_resolves_numeric_arg_from_last_results(isolated_omm_home, monkeypatch):
    from omm import registry

    filename = "a.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"data")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda **_: [filename])

    result = runner.invoke(cli.app, ["uninstall", "1"])

    assert result.exit_code == 0, result.stdout
    assert f"Removed {filename}" in result.stdout
