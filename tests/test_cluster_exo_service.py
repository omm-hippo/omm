"""LaunchAgent that runs the staged exo backend (#503 spike findings)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from omm.cluster import exo_service, preflight

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="omm cluster is macOS-only (launchd, pf, POSIX paths and modes)"
)


def _ok(argv, **_k):
    return subprocess.CompletedProcess(argv, 0, "", "")


def _plist(home: Path) -> dict:
    return plistlib.loads((home / "Library/LaunchAgents/com.omm.cluster-exo.plist").read_bytes())


def test_exo_agent_runs_staged_runtime_with_namespace_through_the_port_script(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    exo_service.install("omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d", run=_ok)
    data = _plist(tmp_path)
    runtime = str(preflight.runtime_dir())
    index = isolated_omm_home / "cluster" / "exo-port-index"
    assert data["ProgramArguments"] == ["/bin/sh", "-c", exo_service.LAUNCH_SCRIPT, "omm-cluster-exo",
                                        str(index), f"{runtime}/exo"]
    assert index.parent.is_dir()
    env = data["EnvironmentVariables"]
    assert env["EXO_LIBP2P_NAMESPACE"] == env["EXO_ZENOH_NAMESPACE"] == "omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d"
    assert env["EXO_RUNTIME_DIR"] == runtime
    assert {"/usr/sbin", "/sbin", runtime, f"{runtime}/_internal"} <= set(env["PATH"].split(":"))
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["WorkingDirectory"] == str(tmp_path / ".exo")
    assert (tmp_path / ".exo").is_dir()


def test_exo_agent_reinstall_boots_out_first(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    run = lambda argv, **k: calls.append(argv) or _ok(argv)
    exo_service.install("omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d", run=run)
    exo_service.install("omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d", run=run)
    assert [c[1] for c in calls if c[0] == "launchctl"] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_exo_agent_uninstall(isolated_omm_home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    exo_service.install("omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d", run=_ok)
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


def test_exo_agent_restart_kickstarts_only_the_exo_job():
    import os

    calls = []
    exo_service.restart(run=lambda argv, **k: calls.append(argv) or _ok(argv))
    assert calls == [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.omm.cluster-exo"]]


def test_exo_agent_restart_fails_when_the_job_is_not_installed():
    import pytest

    from omm.cluster.errors import ClusterError

    run = lambda argv, **k: subprocess.CompletedProcess(argv, 113, "", "Could not find service")
    with pytest.raises(ClusterError, match="Could not find service"):
        exo_service.restart(run=run)


def _launch(index: Path, exo: Path) -> str:
    return subprocess.run(["/bin/sh", "-c", exo_service.LAUNCH_SCRIPT, "omm-cluster-exo", str(index), str(exo)],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_every_exo_start_takes_the_next_libp2p_port_and_wraps(isolated_omm_home, tmp_path):
    # exo 1.0.71 makes a new peer id per start and the other Macs dial the old
    # id at its old port for up to 2500 s; on the same port each dial reached
    # the new exo and started an election every 5 s (real rig 2026-11-06).
    exo = tmp_path / "exo"
    exo.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
    exo.chmod(0o755)
    index = isolated_omm_home / "cluster" / "exo-port-index"
    index.parent.mkdir(parents=True)
    # Older omm ran exo on the first port, so the first start here moves past it.
    assert exo_service.libp2p_port() == 52416
    assert _launch(index, exo) == "--libp2p-port 52417"
    assert _launch(index, exo) == "--libp2p-port 52418"
    assert exo_service.libp2p_port() == 52418
    index.write_text("23\n", encoding="utf-8")
    assert _launch(index, exo) == "--libp2p-port 52416"
    assert exo_service.libp2p_port() == 52416
    index.write_text("garbage", encoding="utf-8")
    assert _launch(index, exo) == "--libp2p-port 52417"


def test_libp2p_ports_stay_below_the_gateway_port():
    from omm.cluster import firewall
    from omm.cluster.gateway import GATEWAY_PORT

    assert firewall.LIBP2P_PORTS.start == 52416 and GATEWAY_PORT not in firewall.LIBP2P_PORTS
    assert 52415 not in firewall.LIBP2P_PORTS
