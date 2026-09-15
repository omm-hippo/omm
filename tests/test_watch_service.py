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


def test_windows_install_calls_schtasks_create(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Windows")
    calls = []
    monkeypatch.setattr(
        watch_service.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )

    watch_service.install()

    args = calls[0][0][0]
    assert args[0] == "schtasks"
    assert "/Create" in args
    assert "ommAutoImport" in args


def test_install_raises_on_unsupported_platform(fake_home, isolated_omm_home, monkeypatch):
    monkeypatch.setattr(watch_service.platform, "system", lambda: "Plan9")

    with pytest.raises(RuntimeError):
        watch_service.install()
