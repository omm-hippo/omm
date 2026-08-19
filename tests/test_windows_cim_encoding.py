"""PowerShell CIM queries must survive a non-ASCII device name.

``powershell.exe`` (Windows PowerShell 5.1) encodes redirected stdout with
``[Console]::OutputEncoding``, which defaults to the *console* output code page
it inherited - 949 on a Korean box, 437 on a US OEM one, 65001 only if
something ran ``chcp 65001`` first. So the bytes arriving on the pipe are
neither UTF-8 by default nor stable between machines, and at a code page that
cannot represent the text PowerShell writes literal ``?`` before we ever see
it, destroying the value irrecoverably.

``hardware._PS_FORCE_UTF8`` pins the child's output encoding so the UTF-8
decode on the Python side is actually correct. These tests exercise the real
``powershell.exe`` rather than a mock, because the whole failure mode lives in
the interaction between two processes' encoding defaults - a mocked
``subprocess.run`` cannot reproduce it.

The Korean sample is built from code points inside the PowerShell snippet, so
the command line itself stays pure ASCII and the test cannot accidentally
measure argv encoding instead of stdout encoding.
"""

import ctypes
import json
import subprocess
import sys

import pytest

from omm import hardware

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="powershell.exe is Windows-only"
)

# 가나다 - built from code points so the command line stays ASCII.
_KOREAN = "\uac00\ub098\ub2e4"
_MAKE_KOREAN = "$k = [string]::Concat([char]0xAC00,[char]0xB098,[char]0xB2E4); "
_EMIT = "[pscustomobject]@{Name=$k} | ConvertTo-Json -Compress"


def _powershell_exe():
    import os

    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )


def _raw_stdout(command: str) -> bytes:
    """Run PowerShell the way hardware.py does, but return undecoded bytes."""
    return subprocess.run(  # noqa: subprocess-encoding: bytes mode on purpose - this test is about what the bytes are
        [_powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        timeout=30,
        creationflags=hardware._NO_WINDOW,
    ).stdout


def test_powershell_json_roundtrips_a_korean_value():
    """The real end-to-end path: PowerShell emits Korean, we get it back intact."""
    data = hardware._powershell_json(_MAKE_KOREAN + _EMIT, timeout=30)
    assert data is not None, "powershell.exe produced no parseable JSON"
    assert data["Name"] == _KOREAN
    assert "\ufffd" not in data["Name"]


def test_utf8_pin_is_load_bearing():
    """Without the pin the bytes are not UTF-8, so the pin is what fixes this.

    Guards against someone deleting ``_PS_FORCE_UTF8`` because "PowerShell
    outputs UTF-8 anyway" - on this machine it emphatically does not.
    """
    bare = _raw_stdout(_MAKE_KOREAN + _EMIT)
    pinned = _raw_stdout(_MAKE_KOREAN + hardware._PS_FORCE_UTF8 + _EMIT)

    assert json.loads(pinned.decode("utf-8"))["Name"] == _KOREAN
    if bare == pinned:
        pytest.skip("this machine's PowerShell already defaults to UTF-8 output")
    with pytest.raises(UnicodeDecodeError):
        bare.decode("utf-8")


@pytest.mark.parametrize("code_page", [949, 65001, 437])
def test_pin_does_not_leak_the_callers_console_code_page(code_page):
    """``[Console]::OutputEncoding`` is a SetConsoleOutputCP call underneath.

    On a console the child *shares* with us that permanently switches the
    user's terminal to code page 65001. ``CREATE_NO_WINDOW`` hands the child
    its own console so the pin stays contained - and the output is UTF-8
    regardless of what the caller's console was set to.
    """
    kernel32 = ctypes.windll.kernel32
    original = kernel32.GetConsoleOutputCP()
    if not original or not kernel32.SetConsoleOutputCP(code_page):
        pytest.skip("no console attached, or this code page is not installed")
    try:
        data = hardware._powershell_json(_MAKE_KOREAN + _EMIT, timeout=30)
        after = kernel32.GetConsoleOutputCP()
    finally:
        kernel32.SetConsoleOutputCP(original)

    assert data is not None and data["Name"] == _KOREAN
    assert after == code_page, (
        f"console code page leaked: {code_page} before the CIM probe, {after} after"
    )


def test_real_cim_cpu_and_gpu_names_decode_cleanly():
    """The actual shipping code path against this machine's real hardware."""
    names = [
        str(item.get("Name") or "")
        for class_name in ("Win32_Processor", "Win32_VideoController")
        for item in hardware._windows_cim(class_name, ["Name"])
    ]
    assert names, "CIM returned no CPU or GPU at all"
    for name in names:
        assert "\ufffd" not in name, f"replacement character in device name: {name!r}"
