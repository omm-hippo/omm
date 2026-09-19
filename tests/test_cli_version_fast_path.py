"""`omm --version` fast path - issue #369 (follow-up to #224).

`cli.py` answers a bare `--version` before importing click/typer/rich/the
domain submodules, using only `package_metadata` + `omm.config`. This must
never diverge from the normal (slow, Typer-routed) `--version` handling
further down the same file, which real subprocess invocations exercise
here rather than mocking anything - the whole point is to catch drift
between the two code paths.
"""

import pytest

from tests._boot_parity import capture_reference


def test_bare_version_matches_the_slow_typer_routed_version(tmp_path):
    # `--version` alone hits the fast path (exact argv match); adding an
    # unrelated flag breaks that exact match and falls through to the
    # normal Typer `--version` handling, which does not treat `--quiet`
    # differently either - so both must print the identical line.
    home = str(tmp_path / "home")
    fast = capture_reference(["--version"], omm_home=home)
    slow = capture_reference(["--version", "--quiet"], omm_home=home)

    assert fast.returncode == 0, fast.stderr
    assert slow.returncode == 0, slow.stderr
    assert fast.stdout == slow.stdout
    assert fast.stdout.startswith("omm ")
    assert fast.stderr == slow.stderr == ""


def test_version_with_json_flag_is_not_caught_by_the_fast_path(tmp_path):
    # `--version --json` must keep going through the slow path (it prints
    # a JSON object, not the plain "omm X.Y.Z" line) - the fast path's
    # exact-match guard must not swallow this shape.
    result = capture_reference(["--version", "--json"], omm_home=str(tmp_path / "home"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("{")
    assert '"version"' in result.stdout


@pytest.mark.parametrize("args", [["--version"], ["--version", "--quiet"]])
def test_version_output_is_dynamic_not_a_stale_hardcoded_string(args, tmp_path):
    # pyproject.toml's patch version is bumped by the pre-commit hook on
    # every commit; a fast path that hardcoded a version string would go
    # stale on the very next commit. Confirm it still reads it live, via a
    # second fresh subprocess (not an in-process import - a prior test's
    # monkeypatch of cli._omm_version could otherwise leak a stale answer).
    import os
    import re
    import subprocess
    import sys

    from tests._boot_parity import FIXED_ENV_OVERRIDES

    result = capture_reference(args, omm_home=str(tmp_path / "home"))

    assert result.returncode == 0, result.stderr
    match = re.fullmatch(r"omm (\S+)\n", result.stdout)
    assert match, result.stdout

    env = {**os.environ, **FIXED_ENV_OVERRIDES, "OMM_HOME": str(tmp_path / "home")}
    reference = subprocess.run(
        [sys.executable, "-c", "from omm.cli import _omm_version; print(_omm_version())"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    assert match.group(1) == reference.stdout.strip()
