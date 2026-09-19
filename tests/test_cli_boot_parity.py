"""Determinism guard for `omm --version` / `omm help` output - issue #224.

A future fast-path shim only has something meaningful to byte-match against
if the real Python boot path itself is stable under a fixed environment.
This proves that stability now, before any fast-path exists, and pins the
comparison helper (`tests/_boot_parity.py`) those channel PRs will reuse.
"""

import pytest

from tests._boot_parity import capture_reference


@pytest.mark.parametrize("args", [["--version"], ["help"]])
def test_boot_output_is_byte_stable_across_independent_omm_homes(args, tmp_path):
    # Two distinct, fresh OMM_HOME dirs: a fast-path shim can't replicate
    # anything the real path derives from OMM_HOME state, so if output
    # differs between a first-run home and another first-run home, that
    # difference would silently break byte-parity forever.
    first = capture_reference(args, omm_home=str(tmp_path / "home1"))
    second = capture_reference(args, omm_home=str(tmp_path / "home2"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr


@pytest.mark.parametrize("args", [["--version"], ["help"]])
def test_boot_output_is_byte_stable_across_repeated_calls_on_same_home(args, tmp_path):
    home = str(tmp_path / "home")
    first = capture_reference(args, omm_home=home)
    second = capture_reference(args, omm_home=home)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr
