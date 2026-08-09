from pathlib import Path

from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


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
