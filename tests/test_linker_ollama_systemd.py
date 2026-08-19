"""ollama_models_dir() must resolve to the directory the *running Ollama
daemon* actually reads from. On Linux, the official installer runs
`ollama serve` under systemd as a dedicated `ollama` system user - whose
$HOME differs from the invoking CLI user's. See issue #117: link_ollama()
was writing manifests under the CLI user's home, which the systemd-managed
daemon never scanned, so `ollama list` never showed the linked model.
"""

import platform
import pwd
import subprocess
from pathlib import Path

import pytest

from omm import linker


def _systemctl_show_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["systemctl"], returncode=returncode, stdout=stdout, stderr="")


def test_ollama_models_dir_uses_systemd_service_users_home(monkeypatch, tmp_path):
    """When ollama.service is active under a dedicated system user, the
    daemon's models dir must be resolved from *that* user's home, not the
    invoking process's Path.home()."""
    ollama_home = tmp_path / "usr-share-ollama"
    ollama_home.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)

    def fake_run(args, **kwargs):
        assert args[:2] == ["systemctl", "show"]
        return _systemctl_show_result("ActiveState=active\nUser=ollama\nEnvironment=\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)
    monkeypatch.setattr(pwd, "getpwnam", lambda user: type("pw", (), {"pw_dir": str(ollama_home)})())
    # A different invoking-user home must NOT be picked here.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home-runner")

    assert linker.ollama_models_dir() == ollama_home / ".ollama" / "models"


def test_ollama_models_dir_honors_systemd_units_own_olama_models_env(monkeypatch, tmp_path):
    """If the unit itself sets OLLAMA_MODELS, that's what the daemon uses -
    it must win over both the invoking process's env and the user's home."""
    unit_models_dir = tmp_path / "custom-unit-models"
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "invoking-process-models"))

    def fake_run(args, **kwargs):
        return _systemctl_show_result(f"ActiveState=active\nUser=ollama\nEnvironment=OLLAMA_MODELS={unit_models_dir}\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)

    assert linker.ollama_models_dir() == unit_models_dir


def test_ollama_models_dir_falls_back_when_service_not_active(monkeypatch, tmp_path):
    """No systemd unit running ollama (macOS, Windows, manual `ollama
    serve`, service stopped, ...) -> keep today's behavior untouched."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)

    def fake_run(args, **kwargs):
        return _systemctl_show_result("ActiveState=inactive\nUser=\nEnvironment=\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home-runner")

    assert linker.ollama_models_dir() == tmp_path / "home-runner" / ".ollama" / "models"


def test_ollama_models_dir_falls_back_on_non_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "mac-home")

    assert linker.ollama_models_dir() == tmp_path / "mac-home" / ".ollama" / "models"


def test_ollama_models_dir_falls_back_when_systemctl_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home-runner")

    assert linker.ollama_models_dir() == tmp_path / "home-runner" / ".ollama" / "models"
