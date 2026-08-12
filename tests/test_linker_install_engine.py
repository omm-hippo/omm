import pytest
from pathlib import Path

from omm import linker


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_engine_raises_for_unimplemented_engine():
    with pytest.raises(NotImplementedError):
        linker.install_engine("textgenwebui")


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
    assert linker.has_automated_installer("textgenwebui") is False


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


def test_install_via_package_manager_mac_uses_brew(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, brew_cask="jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "jan"]


def test_install_via_package_manager_mac_without_brew_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: False, brew_cask="jan",
    )

    assert result.status == "unsupported_platform"
    assert "jan.ai/download" in result.message


def test_install_via_package_manager_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, winget_id="Jan.Jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["winget", "install", "-e", "--id", "Jan.Jan", "--silent"]


def test_install_via_package_manager_linux_uses_flatpak(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker._install_via_package_manager(
        key="jan", label="Jan", manual_url="https://jan.ai/download",
        is_installed=lambda: True, flatpak_id="ai.jan.Jan",
    )

    assert result.status == "installed"
    assert calls[0] == ["flatpak", "install", "-y", "flathub", "ai.jan.Jan"]


def test_install_via_package_manager_no_option_for_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker._install_via_package_manager(
        key="anythingllm", label="AnythingLLM", manual_url="https://docs.anythingllm.com/x",
        is_installed=lambda: False, brew_cask="anythingllm", winget_id="MintplexLabs.AnythingLLM",
    )

    assert result.status == "unsupported_platform"


def test_has_automated_installer_true_for_jan():
    assert linker.has_automated_installer("jan") is True


def test_install_engine_jan_dispatches_to_package_manager_helper(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_jan_installed", lambda: True)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("jan")

    assert result == linker.EngineInstallResult("jan", "installed", "Jan installed successfully.")


def test_has_automated_installer_true_for_anythingllm():
    assert linker.has_automated_installer("anythingllm") is True


def test_install_anythingllm_mac_uses_brew_cask(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("anythingllm")

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "anythingllm"]


def test_install_anythingllm_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("anythingllm")

    assert result.status == "installed"
    assert calls[0] == ["winget", "install", "-e", "--id", "MintplexLabs.AnythingLLM", "--silent"]


def test_install_anythingllm_linux_is_unsupported(monkeypatch):
    """The official Linux installer.sh is interactive (sudo AppArmor
    prompt) with no documented non-interactive flag - automating it is
    explicitly out of scope for this plan."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker.install_engine("anythingllm")

    assert result.status == "unsupported_platform"
    assert "anythingllm.com" in result.message


def test_has_automated_installer_true_for_mstystudio():
    assert linker.has_automated_installer("mstystudio") is True


def test_install_mstystudio_mac_uses_brew_cask(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(linker, "is_mstystudio_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("mstystudio")

    assert result.status == "installed"
    assert calls[0] == ["brew", "install", "--cask", "mstystudio"]


def test_install_mstystudio_windows_is_unsupported(monkeypatch):
    """No current winget package exists for Msty Studio - the only one in
    winget-pkgs (CloudStack.Msty) targets the deprecated legacy app and
    must not be used."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")

    result = linker.install_engine("mstystudio")

    assert result.status == "unsupported_platform"
    assert "msty.ai" in result.message


def test_install_mstystudio_linux_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker.install_engine("mstystudio")

    assert result.status == "unsupported_platform"


def test_has_automated_installer_true_for_koboldcpp():
    assert linker.has_automated_installer("koboldcpp") is True


def test_install_koboldcpp_downloads_to_applications_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    linker.find_koboldcpp_binary.cache_clear()
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        # Simulate curl actually creating the file, like the real download would.
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake binary")
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("koboldcpp")

    assert result.status == "installed"
    assert "koboldcpp-mac-arm64" in calls[0][calls[0].index("-o") - 1]
    assert (tmp_path / "koboldcpp" / "koboldcpp").exists()
    linker.find_koboldcpp_binary.cache_clear()


def test_install_koboldcpp_unsupported_arch_is_unsupported_platform(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")  # Intel Mac - no build exists

    result = linker.install_engine("koboldcpp")

    assert result.status == "unsupported_platform"
    assert "koboldcpp" in result.message.lower()


def test_install_koboldcpp_reports_failed_when_still_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    linker.find_koboldcpp_binary.cache_clear()
    # curl "succeeds" (no exception) but never actually writes the file -
    # simulates a network failure that curl itself doesn't turn into an OSError.
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("koboldcpp")

    assert result.status == "failed"
    linker.find_koboldcpp_binary.cache_clear()
