from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import windows_portable


ROOT = Path(__file__).resolve().parents[1]


def test_project_version_requires_the_expected_distribution(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "omm-model"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    assert windows_portable.project_version(pyproject) == "1.2.3"

    pyproject.write_text(
        '[project]\nname = "unrelated"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    with pytest.raises(windows_portable.WindowsPortableError, match="project name"):
        windows_portable.project_version(pyproject)


def test_windows_version_resource_is_exact_and_rejects_invalid_versions():
    resource = windows_portable.windows_version_resource("1.2.345")

    assert "filevers=(1, 2, 345, 0)" in resource
    assert "StringStruct('ProductVersion', '1.2.345')" in resource
    assert "StringStruct('OriginalFilename', 'omm.exe')" in resource

    with pytest.raises(windows_portable.WindowsPortableError):
        windows_portable.windows_version_resource("1.2")
    with pytest.raises(windows_portable.WindowsPortableError):
        windows_portable.windows_version_resource("1.2.70000")


def test_pyinstaller_command_copies_distribution_metadata_and_package_data(tmp_path):
    entry = tmp_path / "entry.py"
    version_file = tmp_path / "version.txt"
    output = tmp_path / "dist"
    work = tmp_path / "work"

    command = windows_portable.pyinstaller_command(entry, version_file, output, work)

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert ["--onefile", "--console"] == [
        flag for flag in command if flag in {"--onefile", "--console"}
    ]
    assert command[command.index("--copy-metadata") + 1] == "omm-model"
    assert command[command.index("--collect-data") + 1] == "omm"
    assert command[command.index("--version-file") + 1] == str(version_file)
    assert command[-1] == str(entry)


def test_package_is_deterministic_and_has_an_exact_allowlist(tmp_path):
    executable = tmp_path / "input.exe"
    executable.write_bytes(b"MZ" + b"portable executable")
    license_file = tmp_path / "LICENSE"
    license_file.write_text("MIT License\n", encoding="utf-8")

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first, first_checksum = windows_portable.package_windows_portable(
        executable, license_file, "1.2.3", first_dir
    )
    second, second_checksum = windows_portable.package_windows_portable(
        executable, license_file, "1.2.3", second_dir
    )

    assert windows_portable.sha256(first) == windows_portable.sha256(second)
    assert first_checksum.read_text(encoding="ascii") == second_checksum.read_text(
        encoding="ascii"
    )
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["omm.exe", "LICENSE.txt"]
        assert archive.read("omm.exe").startswith(b"MZ")


def test_archive_verifier_rejects_extra_files(tmp_path):
    archive = tmp_path / "omm-windows-x64-1.2.3.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("omm.exe", b"MZ executable")
        bundle.writestr("LICENSE.txt", b"MIT")
        bundle.writestr("unexpected.ps1", b"Write-Host unsafe")

    with pytest.raises(windows_portable.WindowsPortableError, match="expected exactly"):
        windows_portable.verify_windows_archive(archive, "1.2.3")


def test_executable_probe_checks_version_and_help(tmp_path, monkeypatch):
    executable = tmp_path / "omm.exe"
    executable.write_bytes(b"MZ executable")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 120,
        }
        output = "omm 1.2.3" if command[-1] == "--version" else "Example usage:"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(windows_portable.subprocess, "run", fake_run)

    windows_portable.validate_executable(executable, "1.2.3")

    assert calls == [
        [str(executable), "--version"],
        [str(executable), "--help"],
    ]


def test_windows_portable_workflow_is_pinned_and_release_gated():
    workflow = (ROOT / ".github/workflows/windows-portable.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "verify-tag" in workflow
    assert "merge-base --is-ancestor HEAD origin/main" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "contents: write" in workflow
    assert 'gh release upload "v${VERSION}" "release-assets/${asset}" --repo' in workflow
    assert "gh release upload" in workflow and "gh release upload --clobber" not in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/attest-build-provenance@43d14bc2b83dec42d39ecae14e916627a18bb661" in workflow


def test_windows_portable_requirements_are_exactly_pinned():
    requirements = (
        ROOT / "requirements-windows-portable.txt"
    ).read_text(encoding="utf-8").splitlines()

    packages = [line for line in requirements if line and not line.startswith("#")]
    assert packages
    assert all("==" in package for package in packages)
