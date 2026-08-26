from typer.testing import CliRunner

from omm import cli, config, onboarding

runner = CliRunner()


def test_bare_omm_runs_wizard_once_on_fresh_tty_install(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_setup_choice", lambda: "run")
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert config.load_config()["onboarding_completed"] is True


def test_bare_omm_later_choice_skips_wizard_and_leaves_onboarding_incomplete(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_setup_choice", lambda: "later")
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert calls == []
    assert config.load_config()["onboarding_completed"] is False


def test_bare_omm_skip_choice_marks_completed_without_running_wizard(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_setup_choice", lambda: "skip")
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert calls == []
    assert config.load_config()["onboarding_completed"] is True


def test_bare_omm_skips_wizard_when_not_a_tty(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert calls == []
    assert config.load_config()["onboarding_completed"] is False


def test_bare_omm_skips_wizard_when_already_completed(isolated_omm_home, monkeypatch):
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    runner.invoke(cli.app, [])

    assert calls == []


def test_setup_command_reruns_wizard_and_marks_completed(isolated_omm_home, monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert config.load_config()["onboarding_completed"] is True


def test_setup_command_leaves_onboarding_incomplete_when_wizard_aborted(
    isolated_omm_home, monkeypatch
):
    """A Ctrl+C/Escape cancel inside the wizard (surfaced as typer.Abort)
    must not let `omm setup` reach `update_config(onboarding_completed=True)`
    - the wizard should retry next time, not be marked done."""
    import typer

    def _aborting_wizard(console):
        raise typer.Abort()

    monkeypatch.setattr(onboarding, "run_wizard", _aborting_wizard)

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code != 0
    assert config.load_config()["onboarding_completed"] is False


def test_bare_omm_leaves_onboarding_incomplete_when_wizard_aborted(
    isolated_omm_home, monkeypatch
):
    import typer

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_setup_choice", lambda: "run")

    def _aborting_wizard(console):
        raise typer.Abort()

    monkeypatch.setattr(onboarding, "run_wizard", _aborting_wizard)

    result = runner.invoke(cli.app, [])

    assert result.exit_code != 0
    assert config.load_config()["onboarding_completed"] is False
