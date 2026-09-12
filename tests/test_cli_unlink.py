from typer.testing import CliRunner

from omm import cli, registry
from omm.linker import LinkError

runner = CliRunner()


def test_unlink_single_runner_only_touches_that_runner(isolated_omm_home, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model")
    registry.save_registry({filename: {"linked": {"ollama": True, "lmstudio": True}}})

    calls = []
    monkeypatch.setattr(
        cli.linker, "unlink_engine", lambda key, fname, entry, **kw: calls.append(key)
    )

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "ollama"])

    assert result.exit_code == 0, result.stdout
    assert calls == ["ollama"]
    entry = registry.load_registry()[filename]
    assert entry["linked"] == {"ollama": False, "lmstudio": True}


def test_unlink_runner_all_touches_every_linked_runner(isolated_omm_home, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model")
    registry.save_registry(
        {filename: {"linked": {"ollama": True, "lmstudio": True, "jan": False}}}
    )

    calls = []
    monkeypatch.setattr(
        cli.linker, "unlink_engine", lambda key, fname, entry, **kw: calls.append(key)
    )

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "all"])

    assert result.exit_code == 0, result.stdout
    assert sorted(calls) == ["lmstudio", "ollama"]
    entry = registry.load_registry()[filename]
    assert entry["linked"] == {"ollama": False, "lmstudio": False, "jan": False}


def test_unlink_bogus_runner_errors(isolated_omm_home):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {"ollama": True}}})

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "bogus"])

    assert result.exit_code == 2
    assert "--runner must be one of" in result.stderr


def test_unlink_not_linked_reports_and_noop(isolated_omm_home, monkeypatch):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {"ollama": False}}})
    monkeypatch.setattr(
        cli.linker,
        "unlink_engine",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "ollama"])

    assert result.exit_code == 0, result.stdout
    assert "isn't linked into" in result.stdout


def test_unlink_runner_all_with_nothing_linked_reports(isolated_omm_home):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {"ollama": False}}})

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "all"])

    assert result.exit_code == 0, result.stdout
    assert "isn't linked into any runner" in result.stdout


def test_unlink_missing_model_errors(isolated_omm_home):
    result = runner.invoke(cli.app, ["unlink", "nothing-here.gguf", "--runner", "ollama"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_unlink_requires_runner_option(isolated_omm_home):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {"ollama": True}}})

    result = runner.invoke(cli.app, ["unlink", filename])

    assert result.exit_code != 0
    assert "--runner" in result.stderr


def test_unlink_link_error_reports_and_exits_nonzero(isolated_omm_home, monkeypatch):
    filename = "model.gguf"
    registry.save_registry({filename: {"linked": {"ollama": True}}})

    def _fail(key, fname, entry, **kw):
        raise LinkError("boom")

    monkeypatch.setattr(cli.linker, "unlink_engine", _fail)

    result = runner.invoke(cli.app, ["unlink", filename, "--runner", "ollama"])

    assert result.exit_code == 1
    assert "boom" in result.stderr
    entry = registry.load_registry()[filename]
    assert entry["linked"] == {"ollama": True}
