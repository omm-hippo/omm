import subprocess
import sys

import pytest

from omm import watch_service


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_service.Path, "home", lambda: tmp_path)
    return tmp_path


def test_darwin_install_writes_plist_and_loads_it(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    plist_path = watch_service._launchd_plist_path()
    assert plist_path.exists()
    content = plist_path.read_text(encoding="utf-8")
    assert "com.omm.autoimport" in content
    assert sys.executable in content
    assert "_auto-import-run" in content
    assert calls[0][0] == (["launchctl", "load", "-w", str(plist_path)],)


def test_darwin_uninstall_unloads_and_removes_plist(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: None)
    watch_service.install()
    assert watch_service._launchd_plist_path().exists()

    watch_service.uninstall()

    assert not watch_service._launchd_plist_path().exists()


def test_darwin_is_installed_reflects_plist_presence(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    assert watch_service.is_installed() is False
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: None)
    watch_service.install()
    assert watch_service.is_installed() is True


def test_linux_install_writes_unit_and_enables_it(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    unit_path = watch_service._systemd_unit_path()
    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert sys.executable in content
    assert "_auto-import-run" in content
    commands = [c[0][0] for c in calls]
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert ["systemctl", "--user", "enable", "--now", "omm-auto-import.service"] in commands


@pytest.fixture
def fake_windows(fake_home, isolated_omm_home, monkeypatch):
    """Windows branch with the registry, process start, and legacy schtasks
    probes replaced by in-memory fakes, so the tests run on every OS."""
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Windows")
    state = {"run_value": None, "started": [], "legacy_task": False, "subprocess": []}
    monkeypatch.setattr(watch_service, "_windows_run_value_read", lambda: state["run_value"])

    def _write(command):
        state["run_value"] = command

    def _delete():
        state["run_value"] = None

    monkeypatch.setattr(watch_service, "_windows_run_value_write", _write)
    monkeypatch.setattr(watch_service, "_windows_run_value_delete", _delete)
    monkeypatch.setattr(watch_service, "_start_detached", lambda argv: state["started"].append(argv))
    monkeypatch.setattr(watch_service, "_windows_legacy_task_exists", lambda: state["legacy_task"])
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: state["subprocess"].append((a, k))
    )
    return state


def test_windows_install_registers_run_key_and_starts_watcher_now(fake_windows, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    watch_service.install()

    command = fake_windows["run_value"]
    assert command is not None
    assert "_auto-import-run" in command
    assert "-m omm.cli" in command
    # Registered at sign-in *and* running already, like launchd/systemd.
    assert fake_windows["started"] == [watch_service._windows_service_argv()]
    # No Task Scheduler: `schtasks /Create /SC ONLOGON` needs an elevated shell.
    assert not any("schtasks" in a[0] for a, _ in fake_windows["subprocess"])


def test_windows_install_prefers_windowless_pythonw(fake_windows, monkeypatch, tmp_path):
    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"")
    pythonw = python.parent / "pythonw.exe"
    pythonw.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    watch_service.install()

    assert fake_windows["run_value"].startswith(subprocess.list2cmdline([str(pythonw)]))
    assert fake_windows["started"][0][0] == str(pythonw)


def test_windows_install_falls_back_to_python_without_pythonw(fake_windows, monkeypatch, tmp_path):
    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    watch_service.install()

    assert fake_windows["started"][0][0] == str(python)


def test_windows_install_unregisters_when_immediate_start_fails(fake_windows, monkeypatch):
    def _fail(argv):
        raise OSError("cannot start")

    monkeypatch.setattr(watch_service, "_start_detached", _fail)

    with pytest.raises(OSError):
        watch_service.install()

    assert fake_windows["run_value"] is None
    assert watch_service.is_installed() is False


def test_windows_is_installed_reflects_run_key_or_legacy_task(fake_windows):
    assert watch_service.is_installed() is False
    fake_windows["run_value"] = "whatever"
    assert watch_service.is_installed() is True
    fake_windows["run_value"] = None
    fake_windows["legacy_task"] = True
    assert watch_service.is_installed() is True


def test_windows_uninstall_removes_run_key_legacy_task_and_stops_watcher(fake_windows):
    fake_windows["run_value"] = "whatever"

    watch_service.uninstall()

    assert fake_windows["run_value"] is None
    commands = [a[0] for a, _ in fake_windows["subprocess"]]
    assert ["schtasks", "/Delete", "/TN", "ommAutoImport", "/F"] in commands
    stop = [c for c in commands if c[0] == "powershell"]
    assert len(stop) == 1
    assert "_auto-import-run" in stop[0][-1]
    assert "$_.ProcessId -ne $PID" in stop[0][-1]


def test_service_argv_uses_module_for_source_installs(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert watch_service.service_argv() == [sys.executable, "-m", "omm.cli", "_auto-import-run"]


def test_service_argv_calls_frozen_executable_directly(monkeypatch):
    """The PyInstaller builds ship omm itself as sys.executable; `-m omm.cli`
    would be parsed by omm as an unknown option and the service would never
    start."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/omm/omm")
    assert watch_service.service_argv() == ["/opt/omm/omm", "_auto-import-run"]


def test_darwin_and_linux_definitions_follow_service_argv(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/omm/omm")
    monkeypatch.setattr(watch_service.subprocess, "run", lambda *a, **k: None)

    monkeypatch.setattr(watch_service.platform, "system", lambda: "Darwin")
    watch_service.install()
    plist = watch_service._launchd_plist_path().read_text(encoding="utf-8")
    assert "<string>/opt/omm/omm</string>\n        <string>_auto-import-run</string>" in plist
    assert "omm.cli" not in plist

    monkeypatch.setattr(watch_service.platform, "system", lambda: "Linux")
    watch_service.install()
    unit = watch_service._systemd_unit_path().read_text(encoding="utf-8")
    assert "ExecStart=/opt/omm/omm _auto-import-run" in unit


def test_install_raises_on_unsupported_platform(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Plan9")

    with pytest.raises(RuntimeError):
        watch_service.install()
