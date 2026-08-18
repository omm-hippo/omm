from pathlib import Path

from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_link_engine_flag_only_touches_that_engine(isolated_omm_home, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model")
    registry.save_registry({filename: {"linked": {}}})

    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: key in ("ollama", "lmstudio"))
    calls = []

    def fake_link_engine(key, dest, repo_id=None, ollama_tag=None, force=False):
        calls.append(key)
        return None

    monkeypatch.setattr(cli.linker, "link_engine", fake_link_engine)

    result = runner.invoke(cli.app, ["link", "--engine", "ollama"])

    assert result.exit_code == 0, result.stdout
    assert calls == ["ollama"]


def test_link_engine_bogus_value_errors(isolated_omm_home):
    registry.save_registry({"model.gguf": {"linked": {}}})

    result = runner.invoke(cli.app, ["link", "--engine", "bogus"])

    assert result.exit_code == 2
    assert "--engine must be one of" in result.stderr


def test_link_engine_with_directory_errors(isolated_omm_home, tmp_path):
    registry.save_registry({"model.gguf": {"linked": {}}})

    result = runner.invoke(cli.app, ["link", str(tmp_path / "custom"), "--engine", "ollama"])

    assert result.exit_code == 2
    assert "--engine only applies without a directory argument" in result.stderr


def test_link_custom_directory_links_every_registered_model(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    source = cli.MODELS_DIR / filename
    source.write_bytes(b"model")
    registry.save_registry({filename: {"linked": {}}})
    target = tmp_path / "custom-models"

    result = runner.invoke(cli.app, ["link", str(target)])

    assert result.exit_code == 0, result.stdout
    destination = target / filename
    assert destination.is_symlink() or destination.samefile(source)
    entry = registry.load_registry()[filename]
    assert str(target / filename) in entry["custom_links"]


def test_link_reports_clean_error_when_directory_cannot_be_created(isolated_omm_home, tmp_path, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model")
    registry.save_registry({filename: {"linked": {}}})
    target = tmp_path / "custom-models"

    real_mkdir = Path.mkdir

    def _flaky_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if self == target:
            raise OSError("permission denied")
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _flaky_mkdir)

    result = runner.invoke(cli.app, ["link", str(target)])

    assert result.exit_code == 1
    assert "Could not create" in result.stderr


def test_link_names_the_program_not_the_engine_key(isolated_omm_home, monkeypatch):
    """"lmstudio" is a registry key; "LM Studio" is what the program is
    called, and prose the user reads should say the latter."""
    registry.save_registry({"model.gguf": {"linked": {}}})
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)

    result = runner.invoke(cli.app, ["link", "--engine", "lmstudio"])

    assert result.exit_code == 0, result.stdout
    assert "LM Studio isn't installed on this machine" in result.stdout


def test_engine_label_covers_every_known_engine():
    """`_engine_label` backs messages for all seven linkable engines, not
    just the two the benchmark path can select, so a wrong fallback here
    would rename e.g. Jan to "Ollama" in `omm list` and `omm link`."""
    assert [cli._engine_label(spec.key) for spec in cli.linker.ENGINES] == [
        spec.label for spec in cli.linker.ENGINES
    ]


def test_engine_label_falls_back_to_ollama_for_an_unknown_key():
    assert cli._engine_label("not-an-engine") == "Ollama"
