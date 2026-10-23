from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from omm import config, engine_manager, install_safety as safety, linker


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    for name in ("HOMEBREW_NO_VERIFY_ATTESTATIONS", "HOMEBREW_CURLRC", "HOMEBREW_CURL_INSECURE",
                 "HOMEBREW_API_DOMAIN", "HOMEBREW_BOTTLE_DOMAIN", "HOMEBREW_ARTIFACT_DOMAIN",
                 "HOMEBREW_CASK_GIT_REMOTE", "HOMEBREW_CORE_GIT_REMOTE", "HOMEBREW_CASK_OPTS"):
        monkeypatch.delenv(name, raising=False)


def cask(**changes):
    row = {"token": "ollama-app", "tap": "homebrew/cask", "sha256": "a" * 64,
           "url": "https://ollama.com/installer.zip", "version": "1.2.3"}
    row.update(changes)
    return {"casks": [row]}


def mac(monkeypatch, metadata=None, status="assessments enabled"):
    calls = []
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Darwin"))
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return status if "--status" in args else json.dumps(metadata if metadata is not None else cask())
    monkeypatch.setattr(safety, "_run", run)
    return calls


def test_mac_requires_gatekeeper_official_hash_and_hardens_command(monkeypatch):
    calls = mac(monkeypatch)
    command = safety.prepare("ollama", ["brew", "install", "--cask", "ollama-app"])
    assert command == ["brew", "install", "--cask", "homebrew/cask/ollama-app", "--require-sha", "--quarantine"]
    assert len(calls) == 2
    assert "homebrew/cask/ollama-app" in calls[1][0]


@pytest.mark.parametrize("changes", [
    {"sha256": "no_check"}, {"sha256": None}, {"tap": "untrusted/tap"},
    {"token": "different-app"}, {"url": "http://example.com/app.zip"},
    {"disabled": True}, {"deprecated": True}, {"version": "latest"},
])
def test_mac_unknown_or_unpinned_package_blocks(monkeypatch, changes):
    mac(monkeypatch, cask(**changes))
    with pytest.raises(safety.SafetyError):
        safety.prepare("ollama", ["brew", "install", "--cask", "ollama-app"])


def test_disabled_gatekeeper_and_formula_route_are_not_bypassed(monkeypatch):
    mac(monkeypatch, status="assessments disabled")
    with pytest.raises(safety.SafetyError, match="Gatekeeper"):
        safety.prepare("ollama", ["brew", "install", "--cask", "ollama-app"])
    with pytest.raises(safety.SafetyError, match="cask"):
        safety.prepare("ollama", ["brew", "upgrade", "--formula", "ollama"])


@pytest.mark.parametrize("flag", ["--no-quarantine", "--ignore-security-hash", "--no-gpg-verify"])
def test_verification_bypass_flags_block_before_any_native_call(monkeypatch, flag):
    monkeypatch.setattr(safety, "_run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(safety.SafetyError, match="bypass"):
        safety.prepare("ollama", ["brew", "install", "--cask", "ollama-app", flag])


def test_verification_environment_override_is_not_silently_ignored(monkeypatch):
    monkeypatch.setenv("HOMEBREW_NO_VERIFY_ATTESTATIONS", "1")
    with pytest.raises(safety.SafetyError, match="override"):
        safety.prepare("ollama", ["brew", "install", "--cask", "ollama-app"])


def windows(monkeypatch, *, protected=True, age=1, source="https://cdn.winget.microsoft.com/cache"):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Windows"))
    def run(args, **kwargs):
        if "export" in args:
            return json.dumps({"Arg": source, "Type": "Microsoft.PreIndexed.Package", "Name": "winget", "Identifier": "Microsoft.Winget.Source_8wekyb3d8bbwe"})
        return json.dumps({"AMServiceEnabled": protected, "AntivirusEnabled": protected,
                           "RealTimeProtectionEnabled": protected, "AntivirusSignatureAge": age})
    monkeypatch.setattr(safety, "_run", run)


def test_windows_requires_official_source_and_current_defender(monkeypatch):
    windows(monkeypatch)
    command = [r"C:\winget.exe", "install", "--id", "Ollama.Ollama", "--source", "winget"]
    assert safety.prepare("ollama", command) == command


@pytest.mark.parametrize("kwargs", [{"protected": False}, {"age": 8}, {"age": None}, {"source": "https://evil.example"}])
def test_windows_protection_unknown_or_disabled_blocks(monkeypatch, kwargs):
    windows(monkeypatch, **kwargs)
    with pytest.raises(safety.SafetyError):
        safety.prepare("ollama", ["winget", "install", "--id", "Ollama.Ollama", "--source", "winget"])


def test_linux_verifies_gpg_source_and_loads_fresh_scanner_database(monkeypatch):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(safety.shutil, "which", lambda name: "/clamscan")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return "flathub\thttps://dl.flathub.org/repo/\tsystem" if "remotes" in args else ""
    monkeypatch.setattr(safety, "_run", run)
    safety.prepare("jan", ["flatpak", "install", "-y", "flathub", "ai.jan.Jan"])
    assert calls[-1][0][-1] == "-" and calls[-1][1]["input"] == ""
    assert "--fail-if-cvd-older-than=7" in calls[-1][0]


@pytest.mark.parametrize("row", ["flathub\thttps://evil.example\tsystem", "flathub\thttps://dl.flathub.org/repo/\tno-gpg-verify", ""])
def test_linux_untrusted_unsigned_or_unknown_source_blocks(monkeypatch, row):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(safety, "_run", lambda *a, **k: row)
    with pytest.raises(safety.SafetyError):
        safety.prepare("jan", ["flatpak", "install", "-y", "flathub", "ai.jan.Jan"])


def test_native_check_timeout_error_and_nonzero_are_blocking(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("scan", 1)
    monkeypatch.setattr(safety.subprocess, "run", timeout)
    with pytest.raises(safety.SafetyError, match="timed out"):
        safety._run(["scan"])
    monkeypatch.setattr(safety.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 2, "private detection", ""))
    with pytest.raises(safety.SafetyError) as caught:
        safety._run(["scan"])
    assert "private detection" not in str(caught.value)


def test_mac_checks_actual_bundle_and_records_only_named_checks(monkeypatch, isolated_omm_home, tmp_path):
    target = tmp_path / "Example.app"
    target.mkdir()
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(linker, "engine_app_bundle_path", lambda key: target)
    calls = []
    monkeypatch.setattr(safety, "_run", lambda args, **kwargs: calls.append(args) or "")
    result = safety.verify_installed("ollama")
    assert calls == [["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)],
                     ["/usr/sbin/spctl", "--assess", "--type", "execute", str(target)]]
    assert result["all_vulnerabilities_assessed"] is False
    saved = json.loads((config.OMM_HOME / "security-checks/ollama.json").read_text(encoding="utf-8"))
    assert saved == result and str(target) not in json.dumps(saved)


def test_installer_entry_blocks_execution_when_required_gate_fails(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(linker, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/brew")
    def block(*args):
        raise safety.SafetyError("mandatory test failure")
    monkeypatch.setattr(safety, "prepare", block)
    monkeypatch.setattr(linker, "_stream_subprocess", lambda *a, **k: pytest.fail("installer must not execute"))
    result = linker.install_engine("ollama")
    assert result.status == "failed" and "blocked" in result.message


def test_installer_cannot_claim_success_without_postcheck(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(linker, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(safety, "prepare", lambda key, args: args)
    monkeypatch.setattr(linker, "_stream_subprocess", lambda *a, **k: 0)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    def block(*args):
        raise safety.SafetyError("unsigned application")
    monkeypatch.setattr(safety, "verify_installed", block)
    result = linker.install_engine("ollama")
    assert result.status == "failed" and "files may remain" in result.message
    assert safety.last_result("ollama")["status"] == "failed"


def test_windows_checks_authenticode_then_scans_directory_without_remediation(monkeypatch, isolated_omm_home, tmp_path):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Windows"))
    target = tmp_path / "app/Ollama.exe"
    target.parent.mkdir()
    target.write_bytes(b"fixture, never executed")
    scanner = tmp_path / "programs/Windows Defender/MpCmdRun.exe"
    scanner.parent.mkdir(parents=True)
    scanner.write_bytes(b"fixture, never executed")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "programs"))
    monkeypatch.setattr(linker, "engine_windows_executable", lambda key: target)
    monkeypatch.setattr(safety, "_defender", lambda: None)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return "AUTHENTICODE_VALID" if args[0] == "powershell.exe" else ""
    monkeypatch.setattr(safety, "_run", run)
    result = safety.verify_installed("ollama")
    assert calls[0][1]["env"]["OMM_INSTALL_SECURITY_PATH"] == str(target)
    assert calls[-1][0] == [str(scanner), "-Scan", "-ScanType", "3", "-File", str(target.parent), "-DisableRemediation"]
    assert calls[-1][1]["timeout"] == safety.MAX_SCAN_SECONDS
    assert "authenticode" in result["checks"]


def test_linux_checks_real_app_location_and_refuses_empty_scan(monkeypatch, isolated_omm_home, tmp_path):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(safety.Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / ".local/share/flatpak/app/ai.jan.Jan/x86_64/stable/commit"
    target.mkdir(parents=True)
    monkeypatch.setattr(safety, "_run", lambda args, **kwargs: str(target) if args[0] == "flatpak" else "Scanned files: 10\nInfected files: 0")
    assert "clamav_official_signatures_scan" in safety.verify_installed("jan")["checks"]
    monkeypatch.setattr(safety, "_run", lambda args, **kwargs: str(target) if args[0] == "flatpak" else "Scanned files: 0")
    with pytest.raises(safety.SafetyError, match="any files"):
        safety.verify_installed("jan")


def test_linux_scans_resolved_deployment_without_following_internal_links(monkeypatch, isolated_omm_home, tmp_path):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(safety.Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / ".local/share/flatpak/app/ai.jan.Jan/x86_64/stable/commit"
    target.mkdir(parents=True)
    active = target.with_name("active")
    try:
        active.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return str(active) if args[0] == "flatpak" else "Scanned files: 10\nInfected files: 0"
    monkeypatch.setattr(safety, "_run", run)
    safety.verify_installed("jan")
    assert calls[-1][-1] == str(target.resolve())
    assert "--follow-dir-symlinks=0" in calls[-1]


def test_linux_missing_scanner_prevents_deployment(monkeypatch):
    monkeypatch.setattr(safety, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(safety, "_run", lambda *args, **kwargs: "flathub\thttps://dl.flathub.org/repo/\tsystem")
    monkeypatch.setattr(safety.shutil, "which", lambda name: None)
    with pytest.raises(safety.SafetyError, match="ClamAV"):
        safety.prepare("jan", ["flatpak", "install", "-y", "flathub", "ai.jan.Jan"])


def test_update_cannot_bypass_gate_and_uninstall_does_not_need_scanner(monkeypatch, isolated_omm_home):
    receipt = engine_manager.PackageReceipt("brew", "/brew", "ollama-app", "1.0", kind="cask")
    monkeypatch.setattr(engine_manager, "package_receipt", lambda key: receipt)
    def block(*args):
        raise safety.SafetyError("gate blocked")
    monkeypatch.setattr(safety, "prepare", block)
    monkeypatch.setattr(engine_manager, "_run_action", lambda *a: pytest.fail("must not execute"))
    with pytest.raises(engine_manager.EngineManagementError, match="blocked"):
        engine_manager.execute_action(engine_manager.plan_action("ollama", "update"), on_output=lambda line: None)
