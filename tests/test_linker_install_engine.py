import sys

import pytest
import requests
from pathlib import Path

from omm import linker


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_engine_raises_for_unimplemented_engine():
    """Tests the dispatch function's fallback for a key that isn't wired
    up at all - not any particular real engine (every linker.ENGINES key
    has automation now), so this uses an obviously-synthetic key that can
    never collide with a real EngineSpec."""
    with pytest.raises(NotImplementedError):
        linker.install_engine("totally-unknown-engine")


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


def test_find_ollama_executable_windows_finds_documented_location_when_path_stale(
    tmp_path, monkeypatch
):
    """winget updates the registry PATH, but the already-running `omm setup`
    process keeps the PATH it started with - `shutil.which` alone stays
    blind to a just-finished install until the terminal restarts."""
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)

    assert linker.find_ollama_executable() == executable


def test_is_ollama_installed_windows_true_when_only_stale_path_location_found(
    tmp_path, monkeypatch
):
    """Directly reproduces the `omm setup` wizard reporting a just-completed
    winget install as failed: no `~/.ollama` yet, and PATH not refreshed in
    this process, but the binary is already on disk where winget put it."""
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setattr(linker.Path, "home", lambda: tmp_path / "not-home")

    assert linker.is_ollama_installed() is True


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
    """Same rationale as test_install_engine_raises_for_unimplemented_engine
    above: this tests the fallback branch, not a specific real engine."""
    assert linker.has_automated_installer("totally-unknown-engine") is False


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
        is_installed=lambda: False, brew_cask="anythingllm", winget_id="Example.Package",
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


def test_has_automated_installer_true_for_anythingllm_on_mac(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    assert linker.has_automated_installer("anythingllm") is True


def test_has_automated_installer_false_for_anythingllm_on_windows_and_linux(monkeypatch):
    """Brew-cask only. The winget package MintplexLabs.AnythingLLM was
    removed from the community repo in 2025 (microsoft/winget-pkgs#230632)
    and no replacement manifest exists under any publisher id, and no
    flatpak/Linux path was ever built (see _install_anythingllm) - the
    onboarding checklist must not claim auto-install is available on
    either platform."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    assert linker.has_automated_installer("anythingllm") is False
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    assert linker.has_automated_installer("anythingllm") is False


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


def test_install_anythingllm_windows_is_unsupported_without_running_winget(monkeypatch):
    """The winget package MintplexLabs.AnythingLLM was removed from the
    community repo on 2025-02-18 (microsoft/winget-pkgs#230632, "New
    installer URL is behind captcha") and nothing replaced it, so a winget
    install can only ever fail with "no applications found". Windows must
    report unsupported_platform with the manual download link instead of
    spawning winget at all."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    monkeypatch.setattr(linker, "is_anythingllm_installed", lambda: False)

    def fail_popen(*args, **kwargs):
        raise AssertionError("no subprocess may be spawned for AnythingLLM on Windows")

    monkeypatch.setattr(linker.subprocess, "Popen", fail_popen)

    result = linker.install_engine("anythingllm")

    assert result.status == "unsupported_platform"
    assert "anythingllm.com" in result.message


def test_install_anythingllm_linux_is_unsupported(monkeypatch):
    """The official Linux installer.sh is interactive (sudo AppArmor
    prompt) with no documented non-interactive flag - automating it is
    explicitly out of scope for this plan."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    result = linker.install_engine("anythingllm")

    assert result.status == "unsupported_platform"
    assert "anythingllm.com" in result.message


def test_has_automated_installer_true_for_mstystudio(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    assert linker.has_automated_installer("mstystudio") is True


def test_has_automated_installer_false_for_mstystudio_on_windows_and_linux(monkeypatch):
    """Brew-cask only - no winget package targets current Msty Studio (the
    only winget entry targets the deprecated pre-rebrand app) and no Linux
    package manager exists at all."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    assert linker.has_automated_installer("mstystudio") is False
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    assert linker.has_automated_installer("mstystudio") is False


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


def test_has_automated_installer_true_for_koboldcpp(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    assert linker.has_automated_installer("koboldcpp") is True


def test_has_automated_installer_false_for_koboldcpp_unsupported_platform(monkeypatch):
    """No koboldcpp build exists for Intel Mac - reuses
    _KOBOLDCPP_ASSET_BY_PLATFORM directly rather than duplicating the tuple
    list, so this must return False for any (system, machine) not in it."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    assert linker.has_automated_installer("koboldcpp") is False


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


def test_install_koboldcpp_truncated_download_is_cleaned_up_and_reported_failed(tmp_path, monkeypatch):
    """Reproduces the reviewer's live-verified bug: curl can write a
    partial file and still exit nonzero (e.g. a mid-transfer timeout
    against the real koboldcpp asset). That partial file's name alone
    satisfies is_koboldcpp_installed()'s detection, so a truncated/corrupt
    binary must never reach that check - it must be deleted and reported
    "failed" first."""
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    linker.find_koboldcpp_binary.cache_clear()

    def fake_stream_subprocess(args, on_output):
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"partial-truncated-bytes")  # far short of a real binary
        return 18  # curl's real "transfer closed with outstanding read data" code

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    result = linker.install_engine("koboldcpp")

    assert result.status == "failed"
    assert not (tmp_path / "koboldcpp" / "koboldcpp").exists()
    linker.find_koboldcpp_binary.cache_clear()


def test_has_automated_installer_true_for_textgenwebui(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    assert linker.has_automated_installer("textgenwebui") is True
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    assert linker.has_automated_installer("textgenwebui") is True


def test_has_automated_installer_false_for_textgenwebui_on_arm_linux(monkeypatch):
    """The real release only has one narrow ARM build
    (linux-arm64-cuda13.1) - not supported, so this must be False rather
    than guessing an x86_64 asset name for an ARM machine."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "aarch64")
    assert linker.has_automated_installer("textgenwebui") is False


def test_extract_textgenwebui_archive_handles_zip(tmp_path):
    """Real zip bytes, no mocking - exercises the .zip branch and the
    top-level-folder inference for real."""
    import zipfile

    archive_path = tmp_path / "textgen-portable-4.9-windows-cpu.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("textgen-4.9/app/server.py", "# fake")
        zf.writestr("textgen-4.9/user_data/models/place-your-models-here.txt", "")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    result = linker._extract_textgenwebui_archive(archive_path, dest_dir)

    assert result == dest_dir / "textgen-4.9"
    assert (result / "app" / "server.py").exists()
    assert (result / "user_data" / "models" / "place-your-models-here.txt").exists()


def test_extract_textgenwebui_archive_handles_tar_gz(tmp_path):
    """Real tar.gz bytes, no mocking - exercises the tarfile branch (the
    else clause covering everything that isn't .zip)."""
    import tarfile

    src_dir = tmp_path / "textgen-4.9"
    (src_dir / "app").mkdir(parents=True)
    (src_dir / "app" / "server.py").write_text("# fake")

    archive_path = tmp_path / "textgen-portable-4.9-linux-cpu.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(src_dir, arcname="textgen-4.9")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    result = linker._extract_textgenwebui_archive(archive_path, dest_dir)

    assert result == dest_dir / "textgen-4.9"
    assert (result / "app" / "server.py").exists()


def test_install_textgenwebui_picks_cpu_variant_with_no_gpu(monkeypatch, tmp_path):
    from omm.hardware import HardwareInfo

    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    # No GPU: use a fake HardwareInfo rather than relying on the real
    # scan_hardware() reflecting "no GPU", which isn't true on every
    # dev/CI machine this test suite runs on (e.g. Apple Silicon laptops
    # always report a GPU regardless of the mocked platform.system()).
    fake_hw = HardwareInfo(
        os_name="Linux", os_version="", cpu="Test CPU",
        ram_total_gb=16.0, ram_available_gb=8.0, unified_memory=False,
        gpu_name=None, vram_total_gb=None, vram_free_gb=None,
    )
    monkeypatch.setattr(linker, "scan_hardware", lambda: fake_hw)

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-linux-cpu.tar.gz", "browser_download_url": "https://example.test/cpu.tar.gz"},
            {"name": "textgen-portable-4.9-linux-cuda12.4.tar.gz", "browser_download_url": "https://example.test/cuda.tar.gz"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())

    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        # Simulate the download landing where curl -o would put it.
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    def fake_extract(archive_path, dest_dir):
        extracted = dest_dir / "textgen-4.9"
        (extracted / "app").mkdir(parents=True)
        (extracted / "app" / "server.py").touch()
        return extracted

    monkeypatch.setattr(linker, "_extract_textgenwebui_archive", fake_extract)

    result = linker.install_engine("textgenwebui")

    assert result.status == "installed"
    assert downloaded_urls == ["https://example.test/cpu.tar.gz"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_picks_cuda_variant_with_nvidia_gpu(monkeypatch, tmp_path):
    from omm.hardware import HardwareInfo

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    fake_hw = HardwareInfo(
        os_name="Windows", os_version="11", cpu="Test CPU",
        ram_total_gb=32.0, ram_available_gb=16.0, unified_memory=False,
        gpu_name="NVIDIA GeForce RTX 4090", vram_total_gb=24.0, vram_free_gb=20.0,
    )
    monkeypatch.setattr(linker, "scan_hardware", lambda: fake_hw)

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-windows-cpu.zip", "browser_download_url": "https://example.test/cpu.zip"},
            {"name": "textgen-portable-4.9-windows-cuda12.4.zip", "browser_download_url": "https://example.test/cuda.zip"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())

    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    def fake_extract(archive_path, dest_dir):
        extracted = dest_dir / "textgen-4.9"
        (extracted / "app").mkdir(parents=True)
        (extracted / "app" / "server.py").touch()
        return extracted

    monkeypatch.setattr(linker, "_extract_textgenwebui_archive", fake_extract)

    result = linker.install_engine("textgenwebui")

    assert result.status == "installed"
    assert downloaded_urls == ["https://example.test/cuda.zip"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_mac_uses_arch_specific_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)
    linker.find_textgenwebui_root.cache_clear()

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-macos-arm64.tar.gz", "browser_download_url": "https://example.test/arm64.tar.gz"},
            {"name": "textgen-portable-4.9-macos-x86_64.tar.gz", "browser_download_url": "https://example.test/x86_64.tar.gz"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())
    downloaded_urls = []

    def fake_stream_subprocess(args, on_output):
        downloaded_urls.append(args[-1])
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake archive")
        return 0

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)
    monkeypatch.setattr(
        linker,
        "_extract_textgenwebui_archive",
        lambda archive_path, dest_dir: (dest_dir / "textgen-4.9"),
    )

    linker.install_engine("textgenwebui")

    assert downloaded_urls == ["https://example.test/arm64.tar.gz"]
    linker.find_textgenwebui_root.cache_clear()


def test_install_textgenwebui_reports_failed_when_asset_not_found(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")

    class _FakeResponse:
        def json(self):
            return {"assets": []}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())

    result = linker.install_engine("textgenwebui")

    assert result.status == "failed"


def test_install_textgenwebui_arm_linux_is_unsupported_platform_without_network_call(monkeypatch):
    """Reproduces the reviewer's live-verified bug: on ARM Linux,
    _textgenwebui_asset_name used to ignore architecture entirely and
    silently match against an x86_64-only build (linux-cpu/linux-cuda12.4/
    linux-rocm7.2). Must report unsupported_platform instead of guessing,
    and must never even reach the network (no requests.get call)."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "aarch64")

    def fail_if_called(*a, **k):
        raise AssertionError("must not call requests.get for an unsupported arch")

    monkeypatch.setattr(requests, "get", fail_if_called)

    result = linker.install_engine("textgenwebui")

    assert result.status == "unsupported_platform"
    assert "text-generation-webui/releases" in result.message


def test_install_textgenwebui_arm_windows_is_unsupported_platform_without_network_call(monkeypatch):
    """Same arch-gating bug, Windows side: ARM64 Windows must not silently
    match an x86_64-only build either."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.platform, "machine", lambda: "ARM64")

    def fail_if_called(*a, **k):
        raise AssertionError("must not call requests.get for an unsupported arch")

    monkeypatch.setattr(requests, "get", fail_if_called)

    result = linker.install_engine("textgenwebui")

    assert result.status == "unsupported_platform"
    assert "text-generation-webui/releases" in result.message


def test_install_textgenwebui_truncated_download_is_cleaned_up_and_reported_failed(monkeypatch, tmp_path):
    """Same class of bug as koboldcpp's (finding 1): curl can write a
    partial archive and still exit nonzero. A truncated archive usually
    fails to extract on its own, but that's incidental - the returncode
    must be checked explicitly and extraction must never even be
    attempted on a known-bad download."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(linker, "_ENGINE_INSTALL_DIR", tmp_path)

    fake_release = {
        "assets": [
            {"name": "textgen-portable-4.9-linux-cpu.tar.gz", "browser_download_url": "https://example.test/cpu.tar.gz"},
        ]
    }

    class _FakeResponse:
        def json(self):
            return fake_release

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse())

    archive_path = tmp_path / "textgen-portable-4.9-linux-cpu.tar.gz"

    def fake_stream_subprocess(args, on_output):
        archive_path.write_bytes(b"partial-truncated-bytes")
        return 18  # curl's real "transfer closed with outstanding read data" code

    monkeypatch.setattr(linker, "_stream_subprocess", fake_stream_subprocess)

    extract_called = []
    monkeypatch.setattr(
        linker,
        "_extract_textgenwebui_archive",
        lambda *a, **k: extract_called.append(True),
    )

    result = linker.install_engine("textgenwebui")

    assert result.status == "failed"
    assert not extract_called
    assert not archive_path.exists()


def test_stream_subprocess_decodes_utf8_regardless_of_locale(tmp_path):
    """Package managers emit UTF-8 when piped; the interpreter's locale must
    not decide how their output is decoded.

    On Korean Windows the locale default is cp949, and winget's first
    UTF-8-encoded Korean line ("찾음 Jan ...") starts with a byte cp949
    cannot decode - `omm setup` died mid-install with UnicodeDecodeError.
    On cp1252 CI runners the same bytes decode silently into mojibake
    instead, which is why this asserts the Korean text round-trips, not
    merely that no exception is raised. The child source goes through a
    file, not argv: non-ASCII argv is itself encoding-hazardous on Windows.
    """
    child = tmp_path / "emit_utf8.py"
    child.write_text(
        "import sys\n"
        "sys.stdout.buffer.write('\ucc3e\uc74c Jan \ubc84\uc804 0.8.4\\n'.encode('utf-8'))\n",
        encoding="utf-8",
    )

    lines = []
    returncode = linker._stream_subprocess(
        [sys.executable, str(child)], lines.append
    )

    assert returncode == 0
    assert lines == ["찾음 Jan 버전 0.8.4"]
