from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def _fake_group(sha256="deadbeef"):
    return SimpleNamespace(
        sha256=sha256,
        display_name="model.gguf",
        size_bytes=1024**3,
        engines=["ollama"],
    )


def test_import_yes_flag_skips_prompts_without_a_tty(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    adopted = []
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: adopted.append(g.sha256) or SimpleNamespace(filename="model.gguf", bytes_saved=0, link_warnings=[]),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert adopted == ["deadbeef"]


def test_import_yes_flag_before_subcommand_skips_prompts(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    adopted = []
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: adopted.append(g.sha256) or SimpleNamespace(filename="model.gguf", bytes_saved=0, link_warnings=[]),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["--yes", "import"])

    assert result.exit_code == 0, result.stdout
    assert adopted == ["deadbeef"]


def test_import_without_yes_errors_without_a_tty(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: (_ for _ in ()).throw(AssertionError("should not adopt")),
    )

    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 1


def test_import_quiet_suppresses_status_lines_but_keeps_the_summary(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: SimpleNamespace(filename="model.gguf", bytes_saved=0, link_warnings=[]),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes", "--quiet"])

    assert result.exit_code == 0, result.stdout
    assert "Found 1 model" not in result.stdout
    assert "Imported model.gguf" not in result.stdout
    assert "Done:" in result.stdout


def test_import_continues_after_one_group_fails(isolated_omm_home, monkeypatch):
    failing_group = _fake_group(sha256="baadf00d")
    ok_group = _fake_group(sha256="deadbeef")
    monkeypatch.setattr(
        cli.scan_import, "find_external_models", lambda extra_path=None: [object(), object()]
    )
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [failing_group, ok_group])

    adopted = []

    def fake_adopt(g):
        if g.sha256 == "baadf00d":
            raise OSError("disk full")
        adopted.append(g.sha256)
        return SimpleNamespace(filename="model.gguf", bytes_saved=0, link_warnings=[])

    monkeypatch.setattr(cli.scan_import, "adopt_group", fake_adopt)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert adopted == ["deadbeef"]
    assert "baadf00d" not in adopted
