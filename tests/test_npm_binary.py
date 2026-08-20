from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "npm_binary", ROOT / "scripts" / "npm_binary.py"
)
assert SPEC is not None and SPEC.loader is not None
npm_binary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(npm_binary)


@pytest.mark.parametrize(
    ("platform_name", "machine", "libc", "target"),
    [
        ("darwin", "arm64", None, "darwin-arm64"),
        ("darwin", "x86_64", None, "darwin-x64"),
        ("linux", "aarch64", "glibc", "linux-arm64-gnu"),
        ("linux", "x86_64", "glibc", "linux-x64-gnu"),
    ],
)
def test_current_target_is_exact(platform_name, machine, libc, target):
    assert npm_binary.current_target(platform_name, machine, libc) == target


def test_current_target_rejects_unsupported_platform_and_libc():
    with pytest.raises(npm_binary.NpmBinaryError, match="do not support"):
        npm_binary.current_target("win32", "AMD64")
    with pytest.raises(npm_binary.NpmBinaryError, match="require glibc"):
        npm_binary.current_target("linux", "x86_64", "musl")


def test_pyinstaller_command_copies_runtime_metadata(tmp_path):
    command = npm_binary.pyinstaller_command(
        tmp_path / "entry.py", tmp_path / "dist", tmp_path / "work"
    )

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--onefile" in command
    assert command[command.index("--copy-metadata") + 1] == "omm-model"
    assert command[command.index("--collect-data") + 1] == "omm"


def test_executable_probe_checks_version_and_help(tmp_path, monkeypatch):
    executable = tmp_path / "omm"
    executable.write_bytes(bytes.fromhex("7f454c46") + b" executable")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = "omm 1.2.3" if command[-1] == "--version" else "Example usage:"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(npm_binary.subprocess, "run", fake_run)
    npm_binary.validate_executable(executable, "linux-x64-gnu", "1.2.3")

    assert calls == [[str(executable), "--version"], [str(executable), "--help"]]


def test_executable_probe_rejects_wrong_format(tmp_path):
    executable = tmp_path / "omm"
    executable.write_bytes(b"not a native executable")

    with pytest.raises(npm_binary.NpmBinaryError, match="does not match target"):
        npm_binary.validate_executable(executable, "linux-x64-gnu", "1.2.3")


def test_executable_probe_reports_captured_failure_output(tmp_path, monkeypatch):
    executable = tmp_path / "omm"
    executable.write_bytes(bytes.fromhex("7f454c46") + b" executable")

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            1,
            command,
            output="startup output",
            stderr="loader failure",
        )

    monkeypatch.setattr(npm_binary.subprocess, "run", fake_run)

    with pytest.raises(npm_binary.NpmBinaryError) as raised:
        npm_binary.validate_executable(executable, "linux-x64-gnu", "1.2.3")

    message = str(raised.value)
    assert "--version" in message
    assert "startup output" in message
    assert "loader failure" in message
