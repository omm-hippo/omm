"""Cluster Macs must never idle-sleep (#503).

A Mac that idle-slept wakes only for ~45 s per network packet (DarkWake) and
goes straight back to Maintenance Sleep, so the node drops out of the cluster
every few minutes. Seen on a real Mac mini 2026-10-22: `pmset -g log`.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from omm.cluster import power
from omm.cluster.errors import ClusterError

FIX = Path(__file__).parent / "fixtures" / "exo"
REAL = (FIX / "pmset_g.txt").read_text(encoding="utf-8")


def _pmset_g(text):
    def run(argv, **_k):
        if argv == ["/usr/bin/pmset", "-g"]:
            return subprocess.CompletedProcess(argv, 0, text, "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def _sleeping_after(minutes):
    return REAL.replace(" sleep                0 (sleep prevented by powerd)", f" sleep                {minutes}")


def test_idle_sleep_minutes_from_real_output_ignores_the_annotation():
    assert power.idle_sleep_minutes(REAL) == 0


def test_idle_sleep_minutes_reads_a_nonzero_value():
    assert power.idle_sleep_minutes(_sleeping_after(10)) == 10


def test_idle_sleep_minutes_unknown_without_a_sleep_line():
    assert power.idle_sleep_minutes("System-wide power settings:\n") is None


def test_keep_awake_saves_the_original_value_then_sets_zero(isolated_omm_home):
    calls = []
    run = _pmset_g(_sleeping_after(1))
    power.keep_awake(run=lambda argv, **k: calls.append(argv) or run(argv, **k))
    assert ["sudo", "/usr/bin/pmset", "-a", "sleep", "0"] in calls
    assert power.saved_path().read_text(encoding="utf-8").strip() == "1"


def test_keep_awake_rerun_keeps_the_first_saved_value(isolated_omm_home):
    power.keep_awake(run=_pmset_g(_sleeping_after(1)))
    power.keep_awake(run=_pmset_g(REAL))
    assert power.saved_path().read_text(encoding="utf-8").strip() == "1"


def test_keep_awake_raises_when_sudo_fails(isolated_omm_home):
    def run(argv, **k):
        if argv[0] == "sudo":
            return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")
        return _pmset_g(_sleeping_after(1))(argv, **k)
    with pytest.raises(ClusterError, match="sleep"):
        power.keep_awake(run=run)


def test_restore_puts_the_saved_value_back_and_forgets_it(isolated_omm_home):
    power.keep_awake(run=_pmset_g(_sleeping_after(10)))
    calls = []
    power.restore(run=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert calls == [["sudo", "/usr/bin/pmset", "-a", "sleep", "10"]]
    assert not power.saved_path().exists()


def test_restore_without_a_saved_value_changes_nothing(isolated_omm_home):
    calls = []
    power.restore(run=lambda argv, **k: calls.append(argv))
    assert calls == []


def test_restore_failure_keeps_the_saved_value(isolated_omm_home):
    power.keep_awake(run=_pmset_g(_sleeping_after(10)))
    with pytest.raises(ClusterError):
        power.restore(run=lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", ""))
    assert power.saved_path().exists()
