import types

from typer.testing import CliRunner

from omm import cli, config, watch, watch_service

runner = CliRunner()


def test_enable_reports_missing_dependency(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    assert "omm-model[watch]" in result.stdout + result.stderr
    assert config.load_config()["auto_import_enabled"] is False


def test_enable_hint_points_pipx_installs_at_pipx_inject(isolated_omm_home, monkeypatch, tmp_path):
    """`pip install "omm-model[watch]"` never reaches a pipx venv (no pip of
    its own), which is how a Windows user could install it and still be told
    to install it on every retry."""
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)
    monkeypatch.setattr(cli.sys, "frozen", False, raising=False)
    # The pipx environment name is the venv directory name; build it with the
    # host's own separators so the test means the same thing on every OS.
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / "pipx" / "venvs" / "omm-model"))
    monkeypatch.setattr(
        cli.package_metadata, "install_source", lambda: cli.package_metadata.InstallSource.PIPX
    )

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    output = " ".join((result.stdout + result.stderr).split())  # undo console wrapping
    assert "pipx inject omm-model watchdog plyer" in output
    assert "pip install" not in output


def test_enable_hint_tells_frozen_builds_the_watcher_is_not_bundled(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "does not bundle the auto-import watcher" in output
    assert "pip install" not in output


def test_enable_hint_uses_omm_own_interpreter_for_plain_pip_installs(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)
    monkeypatch.setattr(cli.sys, "frozen", False, raising=False)
    monkeypatch.setattr(cli.sys, "executable", "/opt/py/bin/python3")
    monkeypatch.setattr(
        cli.package_metadata, "install_source", lambda: cli.package_metadata.InstallSource.PYPI
    )

    result = runner.invoke(cli.app, ["setting", "auto-import", "enable"])

    assert result.exit_code == 1
    output = " ".join((result.stdout + result.stderr).split())  # undo console wrapping
    assert '"/opt/py/bin/python3" -m pip install "omm-model[watch]"' in output


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


def test_first_run_scan_flag_preserves_concurrent_setting_change(isolated_omm_home, monkeypatch):
    """_maybe_auto_import must not clobber a config change committed by
    another process between its load and its write of external_scan_done."""
    config.update_config(usage_stats_policy="enabled", external_scan_done=False)
    monkeypatch.setattr(cli, "_run_import_flow", lambda *a, **k: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    real = cli.load_config

    def racing_load():
        snap = real()
        config.update_config(usage_stats_policy="never")
        return snap

    monkeypatch.setattr(cli, "load_config", racing_load)

    cli._maybe_auto_import(types.SimpleNamespace(invoked_subcommand="list"))

    saved = config.load_config()
    assert saved["external_scan_done"] is True
    assert saved["usage_stats_policy"] == "never"


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
