"""`omm setting web-autostart` - every OS call is mocked; nothing here may
register a real launchd job, systemd unit or Run registry value."""

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from omm import cli, web_autostart


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(web_autostart.Path, "home", lambda: tmp_path / "home")
    return tmp_path / "home"


@pytest.fixture
def no_real_os_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(web_autostart.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    def _no_popen(*a, **k):
        raise AssertionError("tests must not start a real process")

    monkeypatch.setattr(web_autostart.subprocess, "Popen", _no_popen)
    return calls


@pytest.fixture
def fake_registry(monkeypatch):
    state = {"value": None, "started": [], "stopped": []}
    monkeypatch.setattr(web_autostart, "windows_run_value_read", lambda: state["value"])
    monkeypatch.setattr(web_autostart, "windows_run_value_write", lambda v: state.update(value=v))
    monkeypatch.setattr(web_autostart, "windows_run_value_delete", lambda: state.update(value=None))
    monkeypatch.setattr(web_autostart, "_start_detached", lambda argv: state["started"].append(argv))
    monkeypatch.setattr(
        web_autostart, "_windows_stop_started_manager", lambda cmd: state["stopped"].append(cmd)
    )
    return state


def test_service_argv_runs_web_without_opening_a_browser():
    argv = web_autostart.service_argv()
    assert argv[-1] == "web"
    assert "--open" not in argv


def test_entries_never_share_names_with_auto_import():
    from omm import watch_service

    assert web_autostart.LAUNCHD_LABEL != watch_service._LAUNCHD_LABEL
    assert web_autostart.SYSTEMD_UNIT_NAME != watch_service._SYSTEMD_UNIT_NAME
    assert web_autostart.WINDOWS_RUN_VALUE != watch_service._WINDOWS_RUN_VALUE
    assert web_autostart.launchd_wrapper_path() != watch_service._launchd_wrapper_path()


def test_darwin_install_and_uninstall(fake_home, isolated_omm_home, no_real_os_calls, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Darwin")
    assert web_autostart.is_installed() is False

    web_autostart.install()

    plist = web_autostart.launchd_plist_path()
    assert plist.is_relative_to(fake_home)
    content = plist.read_text(encoding="utf-8")
    assert "<string>com.omm.web</string>" in content
    assert "<string>web</string>" in content
    assert "--open" not in content
    # Restart only after a crash, never after a clean "already running" exit.
    assert "<key>SuccessfulExit</key>\n        <false/>" in content
    assert "<integer>30</integer>" in content
    wrapper = web_autostart.launchd_wrapper_path()
    assert str(wrapper) in content
    assert sys.executable in wrapper.read_text(encoding="utf-8")
    assert no_real_os_calls[0][0] == (["launchctl", "load", "-w", str(plist)],)
    assert web_autostart.is_installed() is True

    web_autostart.uninstall()

    assert not plist.exists()
    assert not wrapper.exists()
    assert no_real_os_calls[-1][0] == (["launchctl", "unload", "-w", str(plist)],)
    assert web_autostart.is_installed() is False


def test_darwin_failed_load_leaves_nothing_registered(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Darwin")

    def _fail(*a, **k):
        raise subprocess.CalledProcessError(1, a[0])

    monkeypatch.setattr(web_autostart.subprocess, "run", _fail)

    with pytest.raises(subprocess.CalledProcessError):
        web_autostart.install()

    assert not web_autostart.launchd_plist_path().exists()
    assert not web_autostart.launchd_wrapper_path().exists()


def test_linux_install_and_uninstall(fake_home, isolated_omm_home, no_real_os_calls, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Linux")

    web_autostart.install()

    unit = web_autostart.systemd_unit_path()
    assert unit.is_relative_to(fake_home)
    content = unit.read_text(encoding="utf-8")
    assert "ExecStart=" in content and content.split("ExecStart=")[1].splitlines()[0].endswith(" web")
    assert "Restart=on-failure" in content
    assert "--open" not in content
    commands = [call[0][0] for call in no_real_os_calls]
    assert ["systemctl", "--user", "enable", "--now", "omm-web.service"] in commands
    assert web_autostart.is_installed() is True

    web_autostart.uninstall()

    assert not unit.exists()
    commands = [call[0][0] for call in no_real_os_calls]
    assert ["systemctl", "--user", "disable", "--now", "omm-web.service"] in commands
    assert web_autostart.is_installed() is False


def test_linux_failed_enable_removes_unit(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Linux")

    def _fail(argv, **k):
        if "enable" in argv:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(web_autostart.subprocess, "run", _fail)

    with pytest.raises(subprocess.CalledProcessError):
        web_autostart.install()

    assert not web_autostart.systemd_unit_path().exists()


def test_windows_install_writes_run_value_and_starts_now(fake_registry, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Windows")

    web_autostart.install()

    assert fake_registry["value"] == subprocess.list2cmdline(web_autostart.windows_service_argv())
    assert fake_registry["value"].endswith(" web")
    assert fake_registry["started"] == [web_autostart.windows_service_argv()]
    assert web_autostart.is_installed() is True

    registered = fake_registry["value"]
    web_autostart.uninstall()

    assert fake_registry["value"] is None
    # Stops only the exact registered command line, never a hand-started `omm web`.
    assert fake_registry["stopped"] == [registered]
    assert web_autostart.is_installed() is False


def test_windows_failed_start_removes_run_value(fake_registry, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Windows")

    def _fail(argv):
        raise OSError("cannot start")

    monkeypatch.setattr(web_autostart, "_start_detached", _fail)

    with pytest.raises(OSError):
        web_autostart.install()

    assert fake_registry["value"] is None


def test_windows_stop_script_passes_command_via_environment(monkeypatch):
    seen = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]

    monkeypatch.setattr(web_autostart.subprocess, "run", _run)
    command = '"C:\\Program Files\\x\\pythonw.exe" -m omm.cli web'

    web_autostart._windows_stop_started_manager(command)

    assert command not in " ".join(seen["argv"])
    assert seen["env"]["OMM_WEB_AUTOSTART_COMMAND"] == command
    assert "-eq $target" in seen["argv"][-1]


def test_unsupported_os_is_not_installed(monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Plan9")
    assert web_autostart.is_installed() is False
    with pytest.raises(RuntimeError):
        web_autostart.install()


def test_cli_enable_status_disable_round_trip(fake_registry, monkeypatch):
    monkeypatch.setattr(web_autostart.platform, "system", lambda: "Windows")
    runner = CliRunner()

    status = runner.invoke(cli.app, ["setting", "web-autostart", "status"])
    assert status.exit_code == 0, status.output
    assert "disabled (default)" in status.output

    enabled = runner.invoke(cli.app, ["setting", "web-autostart", "enable"])
    assert enabled.exit_code == 0, enabled.output
    assert "auto-start enabled" in enabled.output
    assert fake_registry["value"] is not None

    again = runner.invoke(cli.app, ["setting", "web-autostart", "enable"])
    assert "already enabled" in again.output
    assert len(fake_registry["started"]) == 1

    status = runner.invoke(cli.app, ["setting", "web-autostart", "status"])
    assert "enabled" in status.output and "disabled (default)" not in status.output

    disabled = runner.invoke(cli.app, ["setting", "web-autostart", "disable"])
    assert disabled.exit_code == 0, disabled.output
    assert "auto-start disabled" in disabled.output
    assert fake_registry["value"] is None


def test_cli_enable_reports_failure(monkeypatch):
    monkeypatch.setattr(web_autostart, "is_installed", lambda: False)

    def _fail():
        raise subprocess.CalledProcessError(1, ["systemctl"])

    monkeypatch.setattr(web_autostart, "install", _fail)

    result = CliRunner().invoke(cli.app, ["setting", "web-autostart", "enable"])

    assert result.exit_code == 1
    assert "Could not enable web manager auto-start" in result.output
