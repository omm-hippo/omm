from typer.testing import CliRunner

from omm import cli, config, watch_service

runner = CliRunner()


def test_enable_reports_missing_dependency(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_watch_dependencies_available", lambda: False)

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
