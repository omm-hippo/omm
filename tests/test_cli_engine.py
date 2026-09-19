import json

from typer.testing import CliRunner

from omm import cli, engine_manager, linker, onboarding

runner = CliRunner()


def _doctor_item(**overrides) -> dict:
    base = {
        "key": "ollama", "label": "Ollama", "installed": False,
        "package": None, "package_error": None, "package_manageable": True,
        "api_status": "diagnostics_unavailable", "runtime_version": None,
        "manual_url": "https://example.invalid",
    }
    base.update(overrides)
    return base


def test_engine_doctor_with_no_args_does_not_fail_on_uninstalled_runners(monkeypatch):
    # #367: checking all 7 runners must not fail just because most of them
    # were never installed on this machine.
    items = [_doctor_item(key=k, installed=False) for k in
              ("ollama", "lmstudio", "jan", "anythingllm", "mstystudio", "koboldcpp", "textgenwebui")]
    monkeypatch.setattr(engine_manager, "inspect_engine", lambda key, **kw: next(i for i in items if i["key"] == key))

    result = runner.invoke(cli.app, ["engine", "doctor"])

    assert result.exit_code == 0, result.output


def test_engine_doctor_named_but_uninstalled_engine_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(engine_manager, "inspect_engine", lambda key, **kw: _doctor_item(installed=False))

    result = runner.invoke(cli.app, ["engine", "doctor", "ollama"])

    assert result.exit_code == 0, result.output


def test_engine_doctor_fails_when_an_installed_engine_has_a_package_error(monkeypatch):
    monkeypatch.setattr(engine_manager, "inspect_engine",
                         lambda key, **kw: _doctor_item(installed=True, package_error="boom", api_status="ready"))

    result = runner.invoke(cli.app, ["engine", "doctor", "ollama"])

    assert result.exit_code == 1, result.output


def test_engine_doctor_fails_when_an_installed_engine_api_is_unreachable(monkeypatch):
    monkeypatch.setattr(engine_manager, "inspect_engine",
                         lambda key, **kw: _doctor_item(installed=True, api_status="server_unavailable"))

    result = runner.invoke(cli.app, ["engine", "doctor", "ollama"])

    assert result.exit_code == 1, result.output


def test_engine_doctor_passes_for_an_installed_engine_that_is_actually_fine(monkeypatch):
    monkeypatch.setattr(engine_manager, "inspect_engine",
                         lambda key, **kw: _doctor_item(installed=True, api_status="ready"))

    result = runner.invoke(cli.app, ["engine", "doctor", "ollama"])

    assert result.exit_code == 0, result.output


def test_engine_install_runs_checklist_and_installs_selection(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: ["ollama"])
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]


def test_engine_install_skips_install_when_nothing_selected(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: [])
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_engine_install_aborts_when_checklist_is_cancelled(monkeypatch):
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: None)
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install"])

    assert result.exit_code != 0
    assert calls == []


def test_engine_install_json_flag_rejects_without_starting_installer(monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: calls.append(True) or [])

    result = runner.invoke(cli.app, ["engine", "install", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["command"] == "engine install"
    assert calls == []


def test_engine_install_with_name_skips_checklist(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    checklist_calls = []
    monkeypatch.setattr(
        onboarding, "run_engine_checklist", lambda console: checklist_calls.append(1)
    )
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install", "ollama"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]
    assert checklist_calls == []


def test_engine_install_with_name_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install", "Ollama"])

    assert result.exit_code == 0, result.output
    assert calls == [["ollama"]]


def test_engine_install_with_name_already_installed_is_a_noop(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: key == "ollama")
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install", "ollama"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "already installed" in result.output


def test_engine_install_with_unknown_name_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding, "install_selected_engines", lambda console, selected: calls.append(selected) or True
    )

    result = runner.invoke(cli.app, ["engine", "install", "nonsense"])

    assert result.exit_code == 2, result.output
    assert calls == []
    assert "engine must be one of" in result.output


def test_engine_install_exits_nonzero_when_installer_fails(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    monkeypatch.setattr(onboarding, "install_selected_engines", lambda console, selected: False)

    result = runner.invoke(cli.app, ["engine", "install", "ollama"])

    assert result.exit_code == 1
