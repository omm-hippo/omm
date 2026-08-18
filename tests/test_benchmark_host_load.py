"""Background host load during a benchmark.

A benchmark taken while other programs are busy reads low, and every sample
reads low by about the same amount - so the dispersion signals omm already
records (tokens_per_sec_min/max, and the v9 MAD/median ratio) describe it as
a tight, trustworthy measurement of a slow machine. These cover the one
reading that can tell the two apart: system-wide CPU utilization sampled
before the benchmark starts, while omm itself is idle.
"""

from __future__ import annotations

import pytest

from omm import cli, hardware


def _cpu_percent_returning(*values: float):
    readings = list(values)

    def fake(interval=None):
        return readings.pop(0)

    return fake


def _flattened(text: str) -> str:
    """Undo the console's word wrapping so assertions can match a phrase."""
    return " ".join(text.split())


def test_cpu_utilization_reports_the_median_of_its_sampling_windows(monkeypatch):
    monkeypatch.setattr(
        hardware.psutil, "cpu_percent", _cpu_percent_returning(4.0, 61.0, 8.0)
    )

    assert hardware.sample_cpu_utilization_percent() == 8.0


def test_cpu_utilization_is_unknown_rather_than_idle_when_psutil_fails(monkeypatch):
    def unavailable(interval=None):
        raise OSError("performance counters are unavailable")

    monkeypatch.setattr(hardware.psutil, "cpu_percent", unavailable)

    assert hardware.sample_cpu_utilization_percent() is None


def test_cpu_utilization_rejects_a_sampling_plan_it_cannot_honor():
    with pytest.raises(ValueError):
        hardware.sample_cpu_utilization_percent(samples=0)
    with pytest.raises(ValueError):
        hardware.sample_cpu_utilization_percent(interval_seconds=0)


def test_background_load_check_warns_when_other_programs_are_using_the_cpu(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli, "sample_cpu_utilization_percent", lambda: 47.0)

    assert cli._background_cpu_load_is_high() is True

    message = _flattened(capsys.readouterr().err)
    assert "47%" in message
    assert "Close heavy programs" in message


def test_background_load_check_stays_quiet_on_an_idle_machine(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sample_cpu_utilization_percent", lambda: 3.0)

    assert cli._background_cpu_load_is_high() is False
    assert capsys.readouterr().err == ""


def test_background_load_check_does_not_call_an_unknown_reading_busy(monkeypatch, capsys):
    monkeypatch.setattr(cli, "sample_cpu_utilization_percent", lambda: None)

    assert cli._background_cpu_load_is_high() is False
    assert capsys.readouterr().err == ""


def test_a_steady_background_load_is_invisible_to_the_dispersion_signals():
    """The measurement in issue #32, run for run.

    The loaded run's three samples agreed with each other to within 4.4%,
    well inside the 0.15 MAD/median threshold that labels a v9 contribution
    `unstable`, while its median sat 33% below the idle run's. Dispersion is
    the wrong instrument for this failure, which is why the guard reads the
    host instead of the samples.
    """
    from omm import contribute_memory

    loaded = [3.18, 3.41, 3.56]
    idle = [5.08, 5.09, 5.15]

    assert contribute_memory.speed_mad_ratio(loaded) <= 0.15
    assert contribute_memory.speed_mad_ratio(idle) <= 0.15
    assert min(loaded) < max(loaded) * 0.95  # the loaded run is not a flat line
    assert max(loaded) < min(idle)  # yet every loaded sample is below every idle one
