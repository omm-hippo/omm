from dataclasses import asdict
import ctypes
import json
import plistlib
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omm import cli, config, engine_manager as manager
from omm.engines import RuntimeHealth
from omm.engine_packages import operation_lock


def brew_receipt(version="1.0"):
    return manager.PackageReceipt("brew", "/usr/local/bin/brew", "ollama-app", version)


def test_brew_receipt_uses_exact_local_package_identity(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    seen = []
    def query(args):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "not-ollama-app 9\nollama-app 1.2.3\n", "")
    monkeypatch.setattr(manager, "_query", query)
    assert manager.package_receipt("ollama").version == "1.2.3"
    assert seen == [["/usr/local/bin/brew", "list", "--cask", "--versions", "ollama-app"],
                    ["/usr/local/bin/brew", "list", "--formula", "--versions", "ollama"]]


def test_brew_cask_query_by_name_survives_a_broken_bare_cask_listing(monkeypatch):
    # Regression for a Homebrew regression where bare `brew list --cask --versions`
    # (no name) fails outright, even though the same command with a name works.
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    def query(args):
        if args[-1] == "ollama-app":
            return subprocess.CompletedProcess(args, 0, "ollama-app 1.2.3\n", "")
        return subprocess.CompletedProcess(args, 1, "", "Error: not installed\n")
    monkeypatch.setattr(manager, "_query", query)
    receipt = manager.package_receipt("ollama")
    assert receipt.kind == "cask" and receipt.version == "1.2.3"


def test_brew_named_query_treats_a_missing_package_as_absent_not_an_error(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 1, "", "Error: not installed\n"))
    assert manager.package_receipt("ollama") is None


def test_brew_formula_install_is_managed_as_a_formula_not_an_app(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ollama 0.30.10\n" if "--formula" in args else "", ""))
    receipt = manager.package_receipt("ollama")
    assert receipt.kind == "formula" and receipt.package_id == "ollama"
    assert manager.command_for("update", receipt) == ["/brew", "upgrade", "--formula", "ollama"]


def test_brew_does_not_arbitrarily_choose_between_formula_and_app(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ollama 0.30.10\n" if "--formula" in args else "ollama-app 0.30.10\n", ""))
    with pytest.raises(manager.EngineManagementError, match="Both"):
        manager.package_receipt("ollama")


def test_probe_failure_is_not_the_same_as_an_absent_package(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: None)
    with pytest.raises(manager.EngineManagementError, match="Could not read"):
        manager.package_receipt("ollama")


@pytest.mark.parametrize("code", [0x8A150014, -1978335212])
def test_winget_only_treats_documented_missing_package_code_as_absent(code, monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Windows"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "winget.exe")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, code, "", ""))
    assert manager.package_receipt("ollama") is None


def test_winget_parses_identity_independently_of_localized_headers(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Windows"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "winget.exe")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "이름  ID  버전\nOllama  Ollama.Ollama  0.12.3\n", ""))
    receipt = manager.package_receipt("ollama")
    assert receipt.package_id == "Ollama.Ollama" and receipt.version == "0.12.3"


def test_flatpak_refuses_ambiguous_installations(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/flatpak")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ai.jan.Jan\t1.0\tuser\nai.jan.Jan\t1.0\tsystem\n", ""))
    with pytest.raises(manager.EngineManagementError, match="Multiple"):
        manager.package_receipt("jan")


@pytest.mark.parametrize("receipt", [brew_receipt(), manager.PackageReceipt("winget", "winget.exe", "Ollama.Ollama", "1"), manager.PackageReceipt("flatpak", "/flatpak", "ai.jan.Jan", "1", "user")])
def test_actions_target_one_exact_package_and_never_purge(receipt):
    for action in ("update", "uninstall"):
        command = manager.command_for(action, receipt)
        assert receipt.package_id in command
        assert not {"--all", "--force", "--purge", "--zap", "--delete-data"} & set(command)


def test_plan_does_not_guess_how_to_remove_an_external_app(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    with pytest.raises(manager.EngineManagementError, match="Cannot identify"):
        manager.plan_action("ollama", "uninstall")


def test_execute_rechecks_package_before_and_after_change(monkeypatch, isolated_omm_home):
    receipts = iter([brew_receipt(), brew_receipt(), brew_receipt("2.0")])
    monkeypatch.setattr(manager, "package_receipt", lambda key: next(receipts))
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    calls = []
    monkeypatch.setattr(manager, "_run_action", lambda command, on_output: calls.append(command) or 0)
    model = config.MODELS_DIR / "keep.gguf"
    model.write_bytes(b"unchanged")
    plan = manager.plan_action("ollama", "update")
    result = manager.execute_action(plan, on_output=lambda line: None)
    assert result["package"]["version"] == "2.0"
    assert len(calls) == 1
    assert model.read_bytes() == b"unchanged"


def test_package_changed_after_preview_is_not_executed(monkeypatch):
    receipts = iter([brew_receipt(), brew_receipt("2.0")])
    monkeypatch.setattr(manager, "package_receipt", lambda key: next(receipts))
    monkeypatch.setattr(manager, "_run_action", lambda *a: pytest.fail("must not execute"))
    plan = manager.plan_action("ollama", "uninstall")
    with pytest.raises(manager.EngineManagementError, match="changed"):
        manager.execute_action(plan, on_output=lambda line: None)


def test_native_success_without_removal_is_a_failure(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: brew_receipt())
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0)
    with pytest.raises(manager.EngineManagementError, match="still installed"):
        manager.execute_action(manager.plan_action("ollama", "uninstall"), on_output=lambda line: None)


def test_shared_lock_blocks_install_while_another_change_is_in_progress(monkeypatch):
    monkeypatch.setattr(manager.linker, "_install_engine_unlocked", lambda *a, **k: pytest.fail("must not install"))
    with operation_lock("ollama"):
        result = manager.linker.install_engine("ollama")
    assert result.status == "failed"


def test_winget_no_update_is_success_only_after_state_is_rechecked(monkeypatch):
    receipt = manager.PackageReceipt("winget", "winget.exe", "Ollama.Ollama", "1.0")
    monkeypatch.setattr(manager, "package_receipt", lambda key: receipt)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0x8A15002B)
    result = manager.execute_action(manager.plan_action("ollama", "update"), on_output=lambda line: None)
    assert result["status"] == "unchanged"


def test_failure_to_query_after_uninstall_does_not_claim_success(monkeypatch):
    calls = []
    def receipt(key):
        calls.append(key)
        if len(calls) == 3:
            raise manager.EngineManagementError("verification unavailable")
        return brew_receipt()
    monkeypatch.setattr(manager, "package_receipt", receipt)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0)
    with pytest.raises(manager.EngineManagementError, match="verification unavailable"):
        manager.execute_action(manager.plan_action("ollama", "uninstall"), on_output=lambda line: None)


def test_installer_failure_reports_detected_application_honestly(monkeypatch):
    monkeypatch.setattr(manager.linker, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.linker.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager.linker, "_stream_subprocess", lambda *a: 1)
    monkeypatch.setattr(manager.linker, "is_ollama_installed", lambda: True)
    result = manager.linker.install_engine("ollama")
    assert result.status == "failed"
    assert "still detected" in result.message


def test_status_separates_installed_app_from_stopped_api(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "OllamaAdapter", lambda: SimpleNamespace(health=lambda: RuntimeHealth(False, failure_reason="server_unavailable")))
    result = manager.inspect_engine("ollama")
    assert result["installed"] is True
    assert result["api_status"] == "server_unavailable"


def test_package_manageable_is_false_only_for_engines_with_no_package_identity(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    # Hermetic: don't let this test's outcome depend on whatever's actually
    # installed on the machine running it.
    monkeypatch.setattr(manager, "_detected_version", lambda key: None)
    assert manager.inspect_engine("ollama", check_api=False)["package_manageable"] is True
    assert manager.inspect_engine("koboldcpp", check_api=False)["package_manageable"] is False
    assert manager.inspect_engine("textgenwebui", check_api=False)["package_manageable"] is False


def test_ollama_cli_version_parses_the_version_flags_output(monkeypatch):
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(
        manager, "_query",
        lambda args: subprocess.CompletedProcess(args, 0, "ollama version is 0.33.1\n", ""),
    )
    assert manager._ollama_cli_version() == "0.33.1"


def test_ollama_cli_version_is_none_when_ollama_is_not_on_path(monkeypatch):
    monkeypatch.setattr(manager.shutil, "which", lambda name: None)
    assert manager._ollama_cli_version() is None


def test_ollama_cli_version_is_none_when_the_command_fails(monkeypatch):
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/ollama")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 1, "", "boom"))
    assert manager._ollama_cli_version() is None


def _write_info_plist(path, **fields):
    bundle = path / "Contents"
    bundle.mkdir(parents=True)
    with (bundle / "Info.plist").open("wb") as plist_file:
        plistlib.dump(fields, plist_file)


def test_macos_bundle_version_reads_cfbundleshortversionstring(tmp_path, monkeypatch):
    app_path = tmp_path / "LM Studio.app"
    _write_info_plist(app_path, CFBundleShortVersionString="0.4.24+1")
    monkeypatch.setattr(manager.linker, "engine_app_bundle_path", lambda key: app_path)

    assert manager._macos_bundle_version("lmstudio") == "0.4.24+1"


def test_macos_bundle_version_is_none_when_no_bundle_is_installed(monkeypatch):
    monkeypatch.setattr(manager.linker, "engine_app_bundle_path", lambda key: None)
    assert manager._macos_bundle_version("lmstudio") is None


def test_macos_bundle_version_is_none_when_info_plist_is_missing_or_unreadable(tmp_path, monkeypatch):
    app_path = tmp_path / "LM Studio.app"  # Contents/Info.plist never written
    monkeypatch.setattr(manager.linker, "engine_app_bundle_path", lambda key: app_path)
    assert manager._macos_bundle_version("lmstudio") is None


def test_detected_version_prefers_the_bundle_over_the_cli_flag(monkeypatch):
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: "0.33.1")
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: pytest.fail("bundle already answered"))
    calls = []
    monkeypatch.setattr(manager, "_ollama_cli_version", lambda: calls.append(1) or "9.9.9")

    assert manager._detected_version("ollama") == "0.33.1"
    assert calls == []  # never needed the weaker fallback


def test_detected_version_falls_back_to_ollama_cli_flag(monkeypatch):
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: None)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: None)
    monkeypatch.setattr(manager, "_ollama_cli_version", lambda: "0.33.1")

    assert manager._detected_version("ollama") == "0.33.1"


def test_detected_version_has_no_cli_fallback_for_non_ollama_engines(monkeypatch):
    # lms --version prints the CLI tool's own build commit, not the LM
    # Studio app version - nothing safe to fall back to there.
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: None)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: None)
    assert manager._detected_version("lmstudio") is None


class _FakeVersionDll:
    """Stands in for version.dll so the ctypes path runs on any OS: answers
    VerQueryValueW by writing a real in-process buffer's address through the
    byref() out-parameters, exactly like the Win32 call does."""

    def __init__(self, values, translation=(0x0409, 0x04B0), size=64):
        self._size = size
        self._buffers = {}
        if translation is not None:
            self._buffers["\\VarFileInfo\\Translation"] = (ctypes.c_ushort * 2)(*translation)
        for sub_block, text in values.items():
            self._buffers[sub_block] = ctypes.create_unicode_buffer(text)

    def GetFileVersionInfoSizeW(self, path, handle):
        return self._size

    def GetFileVersionInfoW(self, path, handle, size, data):
        return 1

    def VerQueryValueW(self, data, sub_block, pointer_ref, length_ref):
        buffer = self._buffers.get(sub_block)
        if buffer is None:
            return 0
        pointer_ref._obj.value = ctypes.addressof(buffer)
        if isinstance(buffer, ctypes.Array) and buffer._type_ is ctypes.c_ushort:
            length_ref._obj.value = ctypes.sizeof(buffer)
        else:
            length_ref._obj.value = len(buffer.value) + 1  # chars incl. NUL, like Win32
        return 1


def _fake_windows(monkeypatch, dll):
    monkeypatch.setattr(manager.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_load_version_dll", lambda: dll)


def test_read_exe_version_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(manager.sys, "platform", "linux")
    monkeypatch.setattr(manager, "_load_version_dll", lambda: pytest.fail("version.dll must not load off Windows"))
    assert manager._read_exe_version("C:/Program Files/Ollama/ollama.exe") is None


def test_read_exe_version_reads_product_version(monkeypatch):
    _fake_windows(monkeypatch, _FakeVersionDll({
        "\\StringFileInfo\\040904b0\\ProductVersion": "0.12.3",
        "\\StringFileInfo\\040904b0\\FileVersion": "0.12.3.0",
    }))
    assert manager._read_exe_version("ollama.exe") == "0.12.3"


def test_read_exe_version_falls_back_to_file_version(monkeypatch):
    _fake_windows(monkeypatch, _FakeVersionDll({
        "\\StringFileInfo\\040904b0\\FileVersion": "0.4.24.1",
    }))
    assert manager._read_exe_version("LM Studio.exe") == "0.4.24.1"


def test_read_exe_version_uses_the_exes_own_translation(monkeypatch):
    # A non-English build lists its own codepage in VarFileInfo\Translation.
    _fake_windows(monkeypatch, _FakeVersionDll(
        {"\\StringFileInfo\\041204b0\\ProductVersion": "1.2.3"}, translation=(0x0412, 0x04B0),
    ))
    assert manager._read_exe_version("Jan.exe") == "1.2.3"


def test_read_exe_version_tries_common_translations_when_the_table_is_missing(monkeypatch):
    _fake_windows(monkeypatch, _FakeVersionDll(
        {"\\StringFileInfo\\000004b0\\ProductVersion": "2.0.0"}, translation=None,
    ))
    assert manager._read_exe_version("app.exe") == "2.0.0"


def test_read_exe_version_is_none_without_a_version_resource(monkeypatch):
    _fake_windows(monkeypatch, _FakeVersionDll({}, size=0))
    assert manager._read_exe_version("no-resource.exe") is None


def test_read_exe_version_is_none_when_version_dll_fails(monkeypatch):
    monkeypatch.setattr(manager.sys, "platform", "win32")

    def broken():
        raise OSError("version.dll unavailable")

    monkeypatch.setattr(manager, "_load_version_dll", broken)
    assert manager._read_exe_version("ollama.exe") is None


@pytest.mark.skipif(sys.platform != "win32", reason="real version.dll only exists on Windows")
def test_read_exe_version_reads_a_real_exe_on_windows():
    # python.exe carries a real version resource (a venv's copy can lag the
    # interpreter's patch level, so only the shape is checked).
    version = manager._read_exe_version(sys.executable)
    assert version is not None and re.match(r"^\d+\.\d+\.\d+", version)


def test_windows_exe_version_is_none_when_no_exe_is_found(monkeypatch):
    monkeypatch.setattr(manager.linker, "engine_windows_executable", lambda key: None)
    monkeypatch.setattr(manager, "_read_exe_version", lambda path: pytest.fail("nothing to read"))
    assert manager._windows_exe_version("lmstudio") is None


def test_windows_exe_version_reads_the_located_exe(monkeypatch, tmp_path):
    exe = tmp_path / "LM Studio.exe"
    monkeypatch.setattr(manager.linker, "engine_windows_executable", lambda key: exe)
    seen = []
    monkeypatch.setattr(manager, "_read_exe_version", lambda path: seen.append(path) or "0.4.24.1")
    assert manager._windows_exe_version("lmstudio") == "0.4.24.1"
    assert seen == [exe]


def test_detected_version_prefers_the_exe_resource_over_the_cli_flag(monkeypatch):
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: None)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: "0.12.3")
    monkeypatch.setattr(manager, "_ollama_cli_version", lambda: pytest.fail("exe resource already answered"))
    assert manager._detected_version("ollama") == "0.12.3"


def test_detected_version_uses_the_exe_resource_for_non_ollama_engines(monkeypatch):
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: None)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: "0.4.24.1" if key == "lmstudio" else None)
    assert manager._detected_version("lmstudio") == "0.4.24.1"


def test_inspect_engine_prefers_the_exe_resource_over_the_apis_version(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_macos_bundle_version", lambda key: None)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: "0.12.3")

    result = manager.inspect_engine("ollama", check_api=False)

    assert result["detected_version"] == "0.12.3"


def test_inspect_engine_winget_receipt_beats_the_exe_resource(monkeypatch):
    winget = manager.PackageReceipt("winget", "C:/winget.exe", "Ollama.Ollama", "0.12.3")
    monkeypatch.setattr(manager, "package_receipt", lambda key: winget)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_windows_exe_version", lambda key: pytest.fail("receipt already answered"))

    result = manager.inspect_engine("ollama", check_api=False)

    assert result["package"]["manager"] == "winget"
    assert result["detected_version"] is None


def test_inspect_engine_falls_back_to_detected_version_when_not_package_managed(monkeypatch):
    # #368: works even with the daemon not running (an official-installer
    # Ollama, or any engine's .app read straight from its own Info.plist,
    # has no brew/winget identity at all).
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_detected_version", lambda key: "0.33.1")

    result = manager.inspect_engine("ollama", check_api=False)

    assert result["detected_version"] == "0.33.1"


def test_inspect_engine_skips_the_detection_probe_when_a_package_is_already_identified(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: brew_receipt())
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    calls = []
    monkeypatch.setattr(manager, "_detected_version", lambda key: calls.append(1) or "0.33.1")

    result = manager.inspect_engine("ollama", check_api=False)

    assert result["detected_version"] is None
    assert calls == []


def test_cli_dry_run_json_never_executes_or_prompts(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: brew_receipt())
    monkeypatch.setattr(manager, "execute_action", lambda *a, **k: pytest.fail("dry run must not execute"))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: pytest.fail("must not prompt"))
    result = CliRunner().invoke(cli.app, ["engine", "uninstall", "ollama", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True


def test_engine_status_has_no_general_prelude_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OMM_HOME", tmp_path / "not-created")
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: False)
    for name in ("_maybe_start_update_check", "_maybe_auto_import", "_maybe_run_onboarding"):
        monkeypatch.setattr(cli, name, lambda *a: pytest.fail("read-only command must skip prelude"))
    result = CliRunner().invoke(cli.app, ["engine", "status", "ollama", "--no-api", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["installed"] is False
    assert not config.OMM_HOME.exists()
