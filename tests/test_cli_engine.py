from typer.testing import CliRunner

from omm import cli, onboarding

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
