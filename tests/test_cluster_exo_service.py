"""LaunchAgent that runs the staged exo backend (#503 spike findings)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess

from omm.cluster import exo_service, preflight


def _ok(argv, **_k):
    return subprocess.CompletedProcess(argv, 0, "", "")


def _plist(home: Path) -> dict:
    return plistlib.loads((home / "Library/LaunchAgents/com.omm.cluster-exo.plist").read_bytes())


def test_exo_agent_runs_staged_runtime_with_namespace_and_fixed_libp2p_port(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    exo_service.install("omm-0a1b2c3d", run=_ok)
    data = _plist(tmp_path)
    runtime = str(preflight.runtime_dir())
    assert data["ProgramArguments"] == [f"{runtime}/exo", "--libp2p-port", "52416"]
    env = data["EnvironmentVariables"]
    assert env["EXO_LIBP2P_NAMESPACE"] == env["EXO_ZENOH_NAMESPACE"] == "omm-0a1b2c3d"
    assert env["EXO_RUNTIME_DIR"] == runtime
    assert {"/usr/sbin", "/sbin", runtime, f"{runtime}/_internal"} <= set(env["PATH"].split(":"))
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["WorkingDirectory"] == str(tmp_path / ".exo")
    assert (tmp_path / ".exo").is_dir()


def test_exo_agent_reinstall_boots_out_first(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    run = lambda argv, **k: calls.append(argv) or _ok(argv)
    exo_service.install("omm-0a1b2c3d", run=run)
    exo_service.install("omm-0a1b2c3d", run=run)
    assert [c[1] for c in calls if c[0] == "launchctl"] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_exo_agent_uninstall(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    exo_service.install("omm-0a1b2c3d", run=_ok)
    exo_service.uninstall(run=_ok)
    assert not (tmp_path / "Library/LaunchAgents/com.omm.cluster-exo.plist").exists()


def test_bootstrap_is_retried_after_the_bootout_race(isolated_omm_home, tmp_path, monkeypatch):
    import pytest

    from omm.cluster import launchd
    from omm.cluster.errors import ClusterError

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codes = iter([5, 5, 0])
    run = lambda argv, **k: subprocess.CompletedProcess(argv, next(codes) if argv[1] == "bootstrap" else 0, "", "")
    sleeps = []
    launchd.install_agent("com.omm.test", {"ProgramArguments": ["/bin/true"]}, run=run, sleep=sleeps.append)
    assert sleeps == [1, 1]
    always = lambda argv, **k: subprocess.CompletedProcess(argv, 5 if argv[1] == "bootstrap" else 0, "", "Input/output error")
    with pytest.raises(ClusterError, match="Input/output error"):
        launchd.install_agent("com.omm.test", {"ProgramArguments": ["/bin/true"]}, run=always, sleep=lambda s: None)
