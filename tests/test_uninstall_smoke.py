import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _managed_home(tmp_path: Path) -> tuple[Path, Path]:
    managed = tmp_path / "custom-omm-home"
    (managed / "sources" / "old").mkdir(parents=True)
    (managed / "models").mkdir()
    (managed / ".omm-managed").write_text("omm installer managed home v1\n")
    (managed / "config.json").write_text("{}\n")
    sentinel = managed / "keep-me.txt"
    sentinel.write_text("user-owned\n")
    return managed, sentinel


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell smoke test")
def test_powershell_purge_preserves_unknown_files_and_refuses_cwd(tmp_path):
    managed, sentinel = _managed_home(tmp_path)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "python.cmd").write_text("@exit /b 1\n")
    env = os.environ.copy()
    env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    env["OMM_HOME"] = str(managed)

    command = f"& '{ROOT / 'uninstall.ps1'}' -Purge; exit $LASTEXITCODE"
    result = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", command,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    assert sentinel.read_text() == "user-owned\n"
    assert not (managed / "models").exists()
    assert not (managed / "sources").exists()
    assert not (managed / "config.json").exists()
    assert not (managed / ".omm-managed").exists()

    unsafe_env = {**env, "OMM_HOME": str(ROOT)}
    refused = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "uninstall.ps1"),
        ],
        cwd=ROOT,
        env=unsafe_env,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell smoke test")
def test_posix_purge_preserves_unknown_files_and_shell_profiles(tmp_path):
    managed, sentinel = _managed_home(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bashrc = home / ".bashrc"
    bashrc.write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("python3", "python", "pipx"):
        executable = stub / name
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(home)
    env["OMM_HOME"] = str(managed)

    subprocess.run(
        ["sh", str(ROOT / "uninstall.sh"), "--purge"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert sentinel.read_text() == "user-owned\n"
    assert bashrc.read_text() == 'export PATH="$HOME/.local/bin:$PATH"\n'
    assert not (managed / "models").exists()
    assert not (managed / "sources").exists()
    assert not (managed / "config.json").exists()
    assert not (managed / ".omm-managed").exists()

    unsafe_env = {**env, "OMM_HOME": str(ROOT)}
    refused = subprocess.run(
        ["sh", str(ROOT / "uninstall.sh")],
        cwd=ROOT,
        env=unsafe_env,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
