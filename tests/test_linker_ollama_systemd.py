"""ollama_models_dir() must resolve to the directory the *running Ollama
daemon* actually reads from. On Linux, the official installer runs
`ollama serve` under systemd as a dedicated `ollama` system user - whose
$HOME differs from the invoking CLI user's. See issue #117: link_ollama()
was writing manifests under the CLI user's home, which the systemd-managed
daemon never scanned, so `ollama list` never showed the linked model.
"""

import platform
import shlex
import subprocess
from pathlib import Path

import pytest

from omm import linker


def _systemctl_show_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["systemctl"], returncode=returncode, stdout=stdout, stderr="")


@pytest.mark.skipif(platform.system() == "Windows", reason="pwd module doesn't exist on Windows")
def test_ollama_models_dir_uses_systemd_service_users_home(monkeypatch, tmp_path):
    """When ollama.service is active under a dedicated system user, the
    daemon's models dir must be resolved from *that* user's home, not the
    invoking process's Path.home()."""
    import pwd  # POSIX-only; module-level import broke Windows CI collection

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
        # systemd shell-quotes each Environment entry, so mirror that here -
        # tmp_path itself can contain spaces (a checkout under "C:\My Repos",
        # a --basetemp with a space), and an unquoted fixture would be lying
        # about what `systemctl show` actually prints. See issue #130.
        entry = shlex.quote(f"OLLAMA_MODELS={unit_models_dir}")
        return _systemctl_show_result(f"ActiveState=active\nUser=ollama\nEnvironment={entry}\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)

    assert linker.ollama_models_dir() == unit_models_dir


@pytest.mark.parametrize(
    "environment",
    [
        "'OLLAMA_MODELS=/mnt/ollama models'",
        '"OLLAMA_MODELS=/mnt/ollama models"',
        "OLLAMA_HOST=127.0.0.1:11434 'OLLAMA_MODELS=/mnt/ollama models'",
    ],
    ids=["single-quoted", "double-quoted", "alongside-other-vars"],
)
def test_ollama_models_dir_handles_unit_env_path_with_spaces(monkeypatch, environment):
    """systemd quotes Environment= entries containing spaces; splitting on
    whitespace instead of unquoting used to truncate the models dir at the
    first space and return "/mnt/ollama" (issue #130)."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)

    def fake_run(args, **kwargs):
        return _systemctl_show_result(f"ActiveState=active\nUser=ollama\nEnvironment={environment}\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)

    assert linker.ollama_models_dir() == Path("/mnt/ollama models")


def test_ollama_models_dir_survives_unbalanced_unit_env_quoting(monkeypatch, tmp_path):
    """Malformed quoting must not raise out of ollama_models_dir() - fall
    back to the naive split rather than taking the whole command down."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)

    def fake_run(args, **kwargs):
        return _systemctl_show_result("ActiveState=active\nUser=ollama\nEnvironment=OLLAMA_MODELS=\"/mnt/models\n")

    monkeypatch.setattr(linker.subprocess, "run", fake_run)

    assert linker.ollama_models_dir() == Path('"/mnt/models')


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
