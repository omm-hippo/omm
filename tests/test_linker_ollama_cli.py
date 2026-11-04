"""`_run_ollama_cli` must return as soon as the `ollama` CLI itself exits.

On Windows, `ollama show` / `ollama create` start the Ollama tray app when no
server answers and hand it the CLI's stdout/stderr. With captured pipes,
`subprocess.run` waited for an end-of-file that only came when the tray app
quit, so `omm link` hung forever and Ctrl+C could not stop it (2026-11-04).
These tests use a real child process that passes its output handles on to a
long-lived grandchild, the same shape as the CLI starting the tray app.
"""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from omm import linker

# Prints the grandchild's pid so the test can stop it afterwards.
_CLI_THAT_LEAVES_A_CHILD = """
import subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno(),
)
print("cli-output", child.pid, flush=True)
print("cli-error", file=sys.stderr, flush=True)
"""

_SLOW_CLI_THAT_LEAVES_A_CHILD = """
import pathlib, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno(),
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _call_with_deadline(func, seconds: float):
    """Run `func` in a thread so a regression fails this test instead of
    hanging the whole suite."""
    outcome = {}

    def target():
        try:
            outcome["value"] = func()
        except BaseException as error:  # re-raised below on the test thread
            outcome["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"_run_ollama_cli was still blocked after {seconds}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def test_run_ollama_cli_returns_when_the_cli_exits_even_if_its_child_keeps_the_output():
    result = _call_with_deadline(
        lambda: linker._run_ollama_cli([sys.executable, "-c", _CLI_THAT_LEAVES_A_CHILD], timeout=30),
        seconds=20,
    )
    try:
        assert result.returncode == 0
        assert result.stdout.startswith("cli-output ")
        assert result.stderr == "cli-error\n"
    finally:
        _stop(int(result.stdout.split()[1]))


def test_run_ollama_cli_times_out_even_if_a_child_keeps_the_output(tmp_path):
    pid_file = tmp_path / "child.pid"

    def call():
        with pytest.raises(subprocess.TimeoutExpired):
            linker._run_ollama_cli(
                [sys.executable, "-c", _SLOW_CLI_THAT_LEAVES_A_CHILD, str(pid_file)], timeout=3
            )

    try:
        _call_with_deadline(call, seconds=20)
    finally:
        if pid_file.exists():
            _stop(int(pid_file.read_text()))


def test_run_ollama_cli_decodes_output_as_utf8_with_replacement():
    script = "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff\\r\\n')"

    result = linker._run_ollama_cli([sys.executable, "-c", script], timeout=30)

    assert result.stdout == "café �\n"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ollama version is 0.40.6\n", "ollama version is 0.40.6"),
        (
            "Warning: could not connect to a running Ollama instance\n"
            "Warning: client version is 0.40.6\n",
            "ollama version is 0.40.6",
        ),
        (
            "ollama version is 0.40.5\nWarning: client version is 0.40.6\n",
            "ollama version is 0.40.5",
        ),
        ("something else entirely\n", "something else entirely"),
        ("", None),
    ],
    ids=["daemon-running", "daemon-stopped", "daemon-older-than-cli", "unknown-format", "empty"],
)
def test_ollama_cli_version_is_stable_whether_or_not_the_daemon_runs(monkeypatch, output, expected):
    monkeypatch.setattr(linker, "find_ollama_executable", lambda: Path("/fixture/ollama"))
    monkeypatch.setattr(
        linker,
        "_run_ollama_cli",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    assert linker._ollama_cli_version() == expected
