"""#423: double-click install entry points for beginners.

packaging/web-launchers/Install-OMM.cmd (Windows) and Install-OMM.command
(macOS) download the existing install.ps1 / install.sh, run them with
OMM_INSTALL_OPEN_WEB=1 so they finish in the browser manager, and keep the
window open with Korean/English guidance on failure."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "packaging" / "web-launchers" / "Install-OMM.cmd"
COMMAND = ROOT / "packaging" / "web-launchers" / "Install-OMM.command"


def _has_korean(line: str) -> bool:
    return any("가" <= ch <= "힣" for ch in line)


def test_windows_entry_is_crlf_utf8_and_switches_codepage_before_korean():
    raw = CMD.read_bytes()
    # Users download this file raw from GitHub; LF-only batch files break
    # cmd.exe label parsing, so every line must end in CRLF.
    assert b"\n" not in raw.replace(b"\r\n", b"")
    lines = raw.decode("utf-8").split("\r\n")
    chcp = lines.index("chcp 65001 >nul")
    korean = [i for i, line in enumerate(lines) if _has_korean(line)]
    assert korean and min(korean) > chcp
    # No Korean inside a parenthesized block: only plain echo/title lines.
    assert all(lines[i].startswith(("echo ", "title ")) for i in korean)
    assert not any(line.lstrip().startswith(("if ", "for ")) and "(" in line for line in lines)


def test_gitattributes_keeps_entry_line_endings_stable():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "packaging/web-launchers/Install-OMM.cmd -text" in text
    assert "packaging/web-launchers/Install-OMM.command text eol=lf" in text


def test_windows_entry_reuses_install_ps1_with_process_scoped_bypass():
    text = CMD.read_bytes().decode("utf-8")
    assert "https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1" in text
    assert "SecurityProtocolType]::Tls12" in text
    assert 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OMM_INSTALL_SCRIPT%"' in text
    assert "Set-ExecutionPolicy" not in text
    assert 'set "OMM_INSTALL_OPEN_WEB=1"' in text
    assert "이 창을 닫지 마세요" in text
    assert "docs/beginner-install.md" in text
    # Autostart is only ever mentioned, never turned on.
    for line in text.split("\r\n"):
        if "web-autostart enable" in line:
            assert line.startswith("echo ")
    # Every exit waits for Enter first so failure text stays readable.
    lines = text.split("\r\n")
    exits = [i for i, line in enumerate(lines) if line.startswith("exit /b")]
    assert exits and all(lines[i - 1] == "pause >nul" for i in exits)


def test_mac_entry_reuses_install_sh_and_waits_before_closing():
    text = COMMAND.read_text(encoding="utf-8")
    assert "\r" not in text
    assert text.startswith("#!/bin/sh\n")
    assert "https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh" in text
    assert 'OMM_INSTALL_OPEN_WEB=1 sh "$script"' in text
    assert "이 창을 닫지 마세요" in text
    assert "docs/beginner-install.md" in text
    for line in text.splitlines():
        if "web-autostart enable" in line:
            assert line.strip().startswith(("'", "#"))
    assert text.count("pause_before_close\n") == 3


def test_mac_entry_is_executable_in_git():
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", str(COMMAND)],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    if not out.strip():
        pytest.skip("file not tracked yet")
    assert out.split()[0] == "100755"


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_mac_entry_is_syntactically_valid():
    result = subprocess.run(["sh", "-n", str(COMMAND)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe entry contract")
@pytest.mark.parametrize("code", [0, 7])
def test_windows_entry_runs_installer_with_open_web_and_reports_result(tmp_path, code):
    record = tmp_path / "env.txt"
    stub = tmp_path / "stub install.ps1"
    stub.write_text(
        f"Set-Content -LiteralPath '{record}' -Value $env:OMM_INSTALL_OPEN_WEB\r\nexit {code}\r\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["OMM_INSTALL_SCRIPT"] = str(stub)  # never download or run the real installer
    env.pop("OMM_INSTALL_DOWNLOADED", None)
    completed = subprocess.run(
        ["cmd", "/c", str(CMD)], input=b"\r\n", capture_output=True, env=env, timeout=120
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    assert record.read_text(encoding="utf-8").strip() == "1"
    assert "이 창을 닫지 마세요" in output
    assert stub.exists()  # a caller-provided script is never deleted
    if code:
        assert completed.returncode == 1
        assert "[실패] OMM 설치가 끝나지 않았습니다(오류 코드 7)" in output
    else:
        assert completed.returncode == 0
        assert "OMM 설치가 끝났습니다" in output
        assert "[실패]" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX entry contract")
@pytest.mark.parametrize("code", [0, 7])
def test_mac_entry_runs_installer_with_open_web_and_reports_result(tmp_path, code):
    record = tmp_path / "env.txt"
    stub = tmp_path / "stub install.sh"
    stub.write_text(
        f'printf "%s" "$OMM_INSTALL_OPEN_WEB" > {shlex.quote(str(record))}\nexit {code}\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["OMM_INSTALL_SCRIPT"] = str(stub)
    completed = subprocess.run(
        ["sh", str(COMMAND)], input=b"\n", capture_output=True, env=env, timeout=60
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    assert record.read_text(encoding="utf-8") == "1"
    assert completed.returncode == code
    assert stub.exists()
    if code:
        assert "[실패] OMM 설치가 끝나지 않았습니다(오류 코드 7)" in output
    else:
        assert "OMM 설치가 끝났습니다" in output


def test_installers_open_web_only_when_opted_in_and_after_verification():
    sh = (ROOT / "install.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")

    sh_block = sh[sh.index('if [ "${OMM_INSTALL_OPEN_WEB:-}" = "1" ]; then'):]
    assert sh.index('echo "Done.') < sh.index(sh_block)
    assert 'if ! "$PIPX_BIN_DIR/omm" web --open; then' in sh_block
    assert "exit" not in sh_block  # a web failure never fails the verified install
    assert sh.count("OMM_INSTALL_OPEN_WEB") == 2  # one comment mention, one guard

    ps1_block = ps1[ps1.index('if ($env:OMM_INSTALL_OPEN_WEB -eq "1") {'):]
    assert ps1.index('Write-Host "Done.') < ps1.index(ps1_block)
    assert '$ommApp = Join-Path $PipxBinDir "omm.exe"' in ps1_block
    assert "& $ommApp web --open" in ps1_block
    assert "Exit-OmmFailure" not in ps1_block
    assert re.search(r"(?<![$\w])exit\b", ps1_block, re.IGNORECASE) is None
    for script in (sh, ps1):
        assert "web-autostart enable" not in script.split("OMM_INSTALL_OPEN_WEB", 1)[1].replace(
            "explicit `omm setting web-autostart enable`", ""
        )
