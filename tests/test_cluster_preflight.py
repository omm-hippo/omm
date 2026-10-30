"""macOS, exo install, runtime staging and RDMA preflight (#503)."""

from __future__ import annotations

from pathlib import Path
import plistlib
import shutil
import subprocess

import pytest

from omm.cluster import preflight
from omm.cluster.errors import ClusterError

FIX = Path(__file__).parent / "fixtures" / "exo"


def _ok(out=""):
    return lambda argv, **k: subprocess.CompletedProcess(argv, 0, out, "")


def test_cluster_error_is_reexported():
    assert preflight.ClusterError is ClusterError


def test_rdma_status_parses_real_outputs(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rdma_ctl")
    for name, expected in (("rdma_ctl_status_enabled.txt", "enabled"), ("rdma_ctl_status_disabled.txt", "disabled")):
        out = (FIX / name).read_text(encoding="utf-8")
        assert preflight.rdma_status(run=_ok(out)) == expected


def test_rdma_status_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert preflight.rdma_status() == "unavailable"


def test_rdma_status_unavailable_on_unexpected_output(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rdma_ctl")
    assert preflight.rdma_status(run=_ok("something else\n")) == "unavailable"


def test_require_supported_macos_rejects_26_1(monkeypatch):
    monkeypatch.setattr(preflight, "macos_version", lambda: (26, 1))
    with pytest.raises(ClusterError, match="26.2"):
        preflight.require_supported_macos()


def test_require_supported_macos_accepts_26_5(monkeypatch):
    monkeypatch.setattr(preflight, "macos_version", lambda: (26, 5))
    preflight.require_supported_macos()


def test_install_exo_runs_exact_brew_cask_command(monkeypatch):
    calls = []
    monkeypatch.setattr(preflight, "_brew", lambda: "/opt/homebrew/bin/brew")
    preflight.install_exo(lambda line: None, run=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert calls == [["/opt/homebrew/bin/brew", "install", "--cask", "exo"]]


def test_brew_found_outside_path(monkeypatch, tmp_path):
    fake = tmp_path / "brew"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(preflight, "_BREW_CANDIDATES", (str(tmp_path / "missing"), str(fake)))
    assert preflight._brew() == str(fake)


def _recording_run(calls, fail=()):
    def run(argv, **kwargs):
        calls.append(argv)
        failed = any(argv[0].endswith(name) for name in fail)
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "boom" if failed else "")
    return run


def _dmg_steps(calls):
    return [argv[0].rsplit("/", 1)[-1] + (" " + argv[1] if argv[0].endswith("hdiutil") else "") for argv in calls]


def test_install_exo_without_brew_installs_the_dmg(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_brew", lambda: None)
    monkeypatch.setattr(preflight, "EXO_APP_PATH", tmp_path / "EXO.app")
    calls, lines = [], []
    preflight.install_exo(lines.append, run=_recording_run(calls))
    assert _dmg_steps(calls) == ["curl", "hdiutil attach", "ditto", "hdiutil detach"]
    assert calls[0][-1] == preflight.EXO_DMG_URL
    assert calls[2][-1] == str(tmp_path / "EXO.app")
    assert any(preflight.EXO_DMG_URL in line for line in lines)


def test_brew_failure_falls_back_to_the_dmg_and_says_why(monkeypatch, tmp_path):
    # node3, 2026-10-30: /opt/homebrew belongs to another macOS account.
    stderr = ("Error: The following directories are not writable by your user:\n"
              "/opt/homebrew/share/zsh\n/opt/homebrew/var/homebrew/locks\n")
    calls, lines = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0].endswith("brew"):
            return subprocess.CompletedProcess(argv, 1, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(preflight, "_brew", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(preflight, "EXO_APP_PATH", tmp_path / "EXO.app")
    preflight.install_exo(lines.append, run=run)
    assert _dmg_steps(calls) == ["brew", "curl", "hdiutil attach", "ditto", "hdiutil detach"]
    said = "\n".join(lines)
    assert "another macOS account" in said and preflight.EXO_DMG_URL in said
    assert "/opt/homebrew/share/zsh" not in said


def test_brew_failure_reason_is_its_last_line():
    assert preflight._brew_failure_reason("==> Downloading\nError: Download failed\n\n") == "Error: Download failed"
    assert preflight._brew_failure_reason("") == "no output"


def test_install_exo_tells_how_long_brew_takes_and_shows_its_output(monkeypatch):
    # node4, 2026-10-30: a silent few-minute download looked like a hang.
    seen, lines = [], []
    monkeypatch.setattr(preflight, "_brew", lambda: "/opt/homebrew/bin/brew")
    preflight.install_exo(lines.append, run=lambda argv, **k: seen.append(k) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert "400 MB" in lines[0]
    assert "stdout" not in seen[0] and not seen[0].get("capture_output")


def test_dmg_download_failure_raises_with_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_brew", lambda: None)
    monkeypatch.setattr(preflight, "EXO_APP_PATH", tmp_path / "EXO.app")
    calls = []
    with pytest.raises(ClusterError, match="https://assets.exolabs.net/EXO-latest.dmg"):
        preflight.install_exo(lambda line: None, run=_recording_run(calls, fail=("curl",)))
    assert _dmg_steps(calls) == ["curl"]


def test_dmg_copy_failure_still_detaches(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "_brew", lambda: None)
    monkeypatch.setattr(preflight, "EXO_APP_PATH", tmp_path / "EXO.app")
    calls = []
    with pytest.raises(ClusterError, match="/Applications|EXO.app"):
        preflight.install_exo(lambda line: None, run=_recording_run(calls, fail=("ditto",)))
    assert _dmg_steps(calls) == ["curl", "hdiutil attach", "ditto", "hdiutil detach"]


def _fake_app(tmp_path, version="1.0.71"):
    app = tmp_path / "EXO.app"
    (app / "Contents" / "Resources" / "exo").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
    return app


def test_exo_version_reads_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "EXO_APP_PATH", _fake_app(tmp_path))
    assert preflight.exo_installed() is True
    assert preflight.exo_version() == "1.0.71"


def test_stage_runtime_copies_without_quarantine_and_skips_when_current(isolated_omm_home, monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "EXO_APP_PATH", _fake_app(tmp_path))
    calls = []

    def run(argv, **k):
        calls.append(argv)
        Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    target = preflight.stage_runtime(run=run)
    assert target == preflight.runtime_dir()
    assert calls == [["/usr/bin/ditto", "--noqtn", str(tmp_path / "EXO.app/Contents/Resources/exo"), str(target)]]
    preflight.stage_runtime(run=run)
    assert len(calls) == 1


def test_stage_runtime_recopies_after_exo_update(isolated_omm_home, monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "EXO_APP_PATH", _fake_app(tmp_path))
    calls = []
    run = lambda argv, **k: calls.append(argv) or Path(argv[-1]).mkdir(parents=True, exist_ok=True) or subprocess.CompletedProcess(argv, 0, "", "")
    preflight.stage_runtime(run=run)
    _fake_app(tmp_path, version="1.0.72")
    preflight.stage_runtime(run=run)
    assert len(calls) == 2


def test_rdma_guide_lists_recovery_steps():
    text = "\n".join(preflight.RDMA_GUIDE)
    assert "rdma_ctl enable" in text and "Recovery" in text
    assert preflight.RDMA_GUIDE[-1] == ("Every Mac must run the exact same macOS build, and each Mac must be "
                                        "cabled to every other Mac with TB5 cables.")
