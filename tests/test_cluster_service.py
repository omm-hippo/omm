"""Gateway LaunchAgent registration (#503)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from omm import cli
from omm.cluster import service

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="omm cluster is macOS-only (launchd, pf, POSIX paths and modes)"
)


def _ok(argv, **_kwargs):
    return subprocess.CompletedProcess(argv, 0, "", "")


def _plist(home: Path) -> dict:
    return plistlib.loads((home / "Library/LaunchAgents/com.omm.cluster-gateway.plist").read_bytes())


def test_plist_runs_hidden_gateway_command_with_keepalive(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    service.install(run=_ok)
    data = _plist(tmp_path)
    assert data["Label"] == "com.omm.cluster-gateway"
    assert data["ProgramArguments"][-1] == "_cluster-gateway-run"
    assert data["KeepAlive"] is True and data["ThrottleInterval"] == 10 and data["RunAtLoad"] is True
    assert service.is_installed()


def test_service_argv_ends_with_hidden_command():
    assert service.service_argv()[-1] == "_cluster-gateway-run"


def test_install_twice_boots_out_before_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return _ok(argv)

    service.install(run=run)
    service.install(run=run)
    assert [c[1] for c in calls if c[0] == "launchctl"] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_uninstall_removes_plist_and_boots_out(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    service.install(run=_ok)
    calls = []
    service.uninstall(run=lambda argv, **k: calls.append(argv) or _ok(argv))
    assert not service.is_installed()
    assert [c[1] for c in calls if c[0] == "launchctl"] == ["bootout"]


def test_is_running_reads_launchctl_state():
    running = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "\tstate = running\n", "")
    stopped = lambda argv, **k: subprocess.CompletedProcess(argv, 113, "", "Could not find service")
    assert service.is_running(run=running) is True
    assert service.is_running(run=stopped) is False


def test_hidden_command_is_in_all_five_skip_sets():
    for name in ("_SKIP_UPDATE_CHECK_SUBCOMMANDS", "_SKIP_ONBOARDING_SUBCOMMANDS", "_SKIP_AUTO_IMPORT_SUBCOMMANDS",
                 "_SKIP_QUEUED_UPLOAD_SUBCOMMANDS", "_INTERNAL_SUBCOMMANDS"):
        assert "_cluster-gateway-run" in getattr(cli, name)


def test_hidden_command_is_registered_and_hidden():
    command = next(c for c in cli.app.registered_commands if c.name == "_cluster-gateway-run")
    assert command.hidden is True


def test_gateway_agent_pins_omm_home_so_it_reads_the_same_tokens(tmp_path, monkeypatch, isolated_omm_home):
    from omm import config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    service.install(run=_ok)
    assert _plist(tmp_path)["EnvironmentVariables"]["OMM_HOME"] == str(config.OMM_HOME)
