"""Shared helper for zero-computation boot output byte-parity checks.

Issue #224: `omm --version` / `omm help` must eventually be answerable by a
fast-path shim (bash trampoline, npm launcher, etc.) without booting Python,
and that shim's output must byte-match the real Python boot path forever.
`omm help` wraps at the detected terminal width (`cli.py`'s `Console()`
autodetects it), so any byte comparison is only meaningful under a fixed
environment - this module pins the one every comparison must share, and any
future fast-path test (pip/pipx, npm, curl channels) should capture its own
output under the same `FIXED_ENV_OVERRIDES` before diffing against
`capture_reference`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

# Terminal width and color capability are the two things rich's Console()
# autodetects that would otherwise make `omm help` output environment-
# dependent. Pinned so a byte comparison has a fixed target.
FIXED_ENV_OVERRIDES = {"COLUMNS": "80", "NO_COLOR": "1"}


@dataclass(frozen=True)
class BootOutput:
    stdout: str
    stderr: str
    returncode: int


def capture_reference(args: list[str], *, omm_home: str) -> BootOutput:
    """Run the real `python -m omm.cli` boot path and capture its output
    under the fixed environment a fast-path shim must match.

    `omm_home` must be a fresh, isolated directory - every invocation
    writes a run-log entry under `OMM_HOME/logs` (see CLAUDE.md's "Run
    log" section), and this must never touch the caller's real `~/.omm`.
    """
    env = {**os.environ, **FIXED_ENV_OVERRIDES, "OMM_HOME": omm_home}
    result = subprocess.run(
        [sys.executable, "-m", "omm.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return BootOutput(result.stdout, result.stderr, result.returncode)
