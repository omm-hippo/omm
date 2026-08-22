from typer.testing import CliRunner

from omm import cli, linker, onboarding

runner = CliRunner()


def test_engine_install_runs_checklist_and_installs_selection(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: ["ollama"])
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]


def test_engine_install_skips_install_when_nothing_selected(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: [])
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_engine_install_aborts_when_checklist_is_cancelled(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: None)
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code != 0
    assert calls == []


def test_engine_install_json_flag_warns_since_unsupported(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: [])

    result = runner.invoke(cli.app, ["engine", "install", "--json"])

    assert "--json has no effect on `omm engine install`" in result.output


def test_engine_install_with_name_skips_checklist(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    checklist_calls = []
    monkeypatch.setattr(
        onboarding, "run_engine_checklist", lambda console: checklist_calls.append(1)
    )
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install", "ollama"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]
    assert checklist_calls == []


def test_engine_install_with_name_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install", "Ollama"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]


def test_engine_install_with_name_already_installed_is_a_noop(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: key == "ollama")
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install", "ollama"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "already installed" in result.output


def test_engine_install_with_unknown_name_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected)
    )

    result = runner.invoke(cli.app, ["engine", "install", "nonsense"])

    assert result.exit_code == 2, result.output
    assert calls == []
    assert "engine must be one of" in result.output
