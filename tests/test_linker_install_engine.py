import pytest

from omm import linker


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_engine_raises_for_unimplemented_engine():
    with pytest.raises(NotImplementedError):
        linker.install_engine("jan")


def test_install_ollama_mac_streams_output_and_reports_installed(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(
        linker.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(["downloading...\n", "done\n"]),
    )
    captured = []

    result = linker.install_engine("ollama", on_output=captured.append)

    assert result == linker.EngineInstallResult(
        "ollama", "installed", "Ollama installed successfully."
    )
    assert captured == ["downloading...", "done"]


def test_install_ollama_linux_reports_failed_when_still_not_detected(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert result.key == "ollama"


def test_install_ollama_handles_popen_start_failure(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    def _raise(*a, **k):
        raise OSError("no /bin/sh")

    monkeypatch.setattr(linker.subprocess, "Popen", _raise)

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert "no /bin/sh" in result.message


def test_install_ollama_windows_without_winget_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker.install_engine("ollama")

    assert result.status == "unsupported_platform"


def test_install_ollama_windows_with_winget_runs_winget_install(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        linker.shutil, "which", lambda name: "C:\\winget.exe" if name == "winget" else None
    )
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("ollama")

    assert result.status == "installed"
    assert calls[0][:3] == ["winget", "install", "-e"]


def test_install_ollama_unknown_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "FreeBSD")

    result = linker.install_engine("ollama")

    assert result.status == "unsupported_platform"


def test_install_ollama_linux_failure_message_includes_manual_link(monkeypatch):
    """Every other failure path already gives a manual next step; the
    mac/linux "installer ran but still not detected" case must too,
    instead of leaving the user stuck with no fallback."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(
        linker.subprocess, "Popen", lambda *a, **k: _FakeProc([], returncode=1)
    )

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert "https://ollama.com/download" in result.message


def test_install_ollama_mac_failure_message_includes_manual_link(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(
        linker.subprocess, "Popen", lambda *a, **k: _FakeProc([], returncode=1)
    )

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert "https://ollama.com/download" in result.message


def test_has_automated_installer_true_for_ollama():
    assert linker.has_automated_installer("ollama") is True


def test_has_automated_installer_false_for_engine_without_installer():
    assert linker.has_automated_installer("jan") is False


def test_is_lmstudio_installed_detects_headless_cli(monkeypatch, tmp_path):
    """A headless llmster install has no GUI app bundle, but is still a
    real, usable install - detection must not require the GUI app."""
    monkeypatch.setattr(linker, "_app_bundle_installed", lambda name: False)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "/usr/local/bin/lms")

    assert linker.is_lmstudio_installed() is True


def test_has_automated_installer_true_for_lmstudio():
    assert linker.has_automated_installer("lmstudio") is True


def test_install_lmstudio_mac_linux_streams_output_and_reports_installed(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(
        linker.subprocess, "Popen", lambda *a, **k: _FakeProc(["Downloading llmster...\n"])
    )
    captured = []

    result = linker.install_engine("lmstudio", on_output=captured.append)

    assert result == linker.EngineInstallResult(
        "lmstudio", "installed", "LM Studio installed successfully."
    )
    assert captured == ["Downloading llmster..."]


def test_install_lmstudio_linux_reports_failed_when_still_not_detected(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("lmstudio")

    assert result.status == "failed"
    assert "lmstudio.ai/download" in result.message


def test_install_lmstudio_windows_uses_powershell(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        linker.shutil, "which", lambda name: "C:\\powershell.exe" if name == "powershell" else None
    )
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("lmstudio")

    assert result.status == "installed"
    assert calls[0][0] == "C:\\powershell.exe"
    assert "irm https://lmstudio.ai/install.ps1 | iex" in calls[0]


def test_install_lmstudio_windows_without_powershell_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker.install_engine("lmstudio")

    assert result.status == "unsupported_platform"
