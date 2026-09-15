import subprocess as real_subprocess

from typer.testing import CliRunner

from omm import cli, config, watch, watch_service

runner = CliRunner()


def test_enable_reports_missing_dependency(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    assert "omm-model[watch]" in result.stdout + result.stderr
    assert config.load_config()["auto_import_enabled"] is False


def test_install_watch_dependencies_declines_without_a_tty(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no tty"))
    )

    assert cli._install_watch_dependencies() is False


def test_install_watch_dependencies_runs_pip_against_own_interpreter(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_is_brew_managed_python", lambda: False)
    pip_calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **k: pip_calls.append(cmd) or real_subprocess.CompletedProcess(cmd, 0),
    )

    assert cli._install_watch_dependencies() is True
    assert pip_calls == [[cli.sys.executable, "-m", "pip", "install", "watchdog>=4", "plyer>=2.1"]]


def test_install_watch_dependencies_declines_when_user_says_no(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pip call"))
    )

    assert cli._install_watch_dependencies() is False


def test_install_watch_dependencies_reports_pip_failure(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_is_brew_managed_python", lambda: False)

    def fake_run(cmd, **kwargs):
        raise real_subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._install_watch_dependencies() is False


def test_enable_offers_to_install_dependencies_when_missing(isolated_omm_home, monkeypatch):
    calls = []

    def fake_available():
        calls.append(True)
        return len(calls) > 1  # missing on first check, present after "install"

    monkeypatch.setattr(cli, "_watch_dependencies_available", fake_available)
    monkeypatch.setattr(cli, "_install_watch_dependencies", lambda: True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: False)
    monkeypatch.setattr(watch_service, "install", lambda: None)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["auto_import_enabled"] is True


def test_enable_declining_install_offer_reports_missing_dependency(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)
    monkeypatch.setattr(cli, "_install_watch_dependencies", lambda: False)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    assert "omm-model[watch]" in result.stdout + result.stderr
    assert config.load_config()["auto_import_enabled"] is False


def test_enable_installs_service_and_sets_flag(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: True)
    installed = []
    monkeypatch.setattr(watch_service, "is_installed", lambda: False)
    monkeypatch.setattr(watch_service, "install", lambda: installed.append(True))

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 0, result.stdout
    assert installed == [True]
    assert config.load_config()["auto_import_enabled"] is True


def test_enable_is_idempotent_when_already_installed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)

    def _fail_install():
        raise AssertionError("install() should not be called when already installed")

    monkeypatch.setattr(watch_service, "install", _fail_install)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 0, result.stdout


def test_disable_uninstalls_service_and_clears_flag(isolated_omm_home, monkeypatch):
    config.update_config(auto_import_enabled=True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)
    uninstalled = []
    monkeypatch.setattr(watch_service, "uninstall", lambda: uninstalled.append(True))

    result = runner.invoke(cli.app, ["setting", "auto-import", "disable"])

    assert result.exit_code == 0, result.stdout
    assert uninstalled == [True]
    assert config.load_config()["auto_import_enabled"] is False


def test_status_shows_enabled_and_registered(isolated_omm_home, monkeypatch):
    config.update_config(auto_import_enabled=True)
    monkeypatch.setattr(watch_service, "is_installed", lambda: True)

    result = runner.invoke(cli.app, ["setting", "auto-import", "status"])

    assert result.exit_code == 0, result.stdout
    assert "enabled" in result.stdout


def test_auto_import_run_cmd_delegates_to_watch_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "run_watch_loop", lambda: calls.append(1))

    result = runner.invoke(cli.app, ["_auto-import-run"])

    assert result.exit_code == 0, result.stdout
    assert calls == [1]


def test_auto_import_run_is_hidden_from_help():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert "_auto-import-run" not in result.stdout


def test_auto_import_run_skips_update_check_onboarding_and_import_offer(
    isolated_omm_home, monkeypatch
):
    """The service process must be a pure watch loop: none of the normal
    root-prelude side effects (update check, first-run onboarding wizard,
    stray-model import offer) should fire just because a real TTY-like
    stdin happens to be attached to the service."""
    monkeypatch.setattr(watch, "run_watch_loop", lambda: None)
    config.update_config(onboarding_completed=False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("_auto-import-run must skip this root-prelude side effect")

    monkeypatch.setattr(cli, "_installed_commit", forbidden)
    monkeypatch.setattr(cli, "_ask_setup_choice", forbidden)
    monkeypatch.setattr(cli, "_run_import_flow", forbidden)

    result = runner.invoke(cli.app, ["_auto-import-run"])

    assert result.exit_code == 0, result.stdout
