from __future__ import annotations

import pytest

from omm import predictor
from omm.hardware import HardwareInfo, calculate_memory_budget


def _hardware(**overrides) -> HardwareInfo:
    values = {
        "os_name": "macOS",
        "os_version": "",
        "cpu": "CPU",
        "ram_total_gb": 24.0,
        "ram_available_gb": 10.0,
        "unified_memory": True,
        "gpu_name": "GPU",
        "vram_total_gb": 24.0,
        "vram_free_gb": 10.0,
        "gpu_tflops": None,
    }
    values.update(overrides)
    return HardwareInfo(**values)


def test_unified_budget_subtracts_live_apps_and_os_reserve_once():
    budget = calculate_memory_budget(_hardware())

    assert budget.model_budget_gb == pytest.approx(7.6)
    assert budget.ram_safety_reserve_gb == pytest.approx(2.4)
    assert budget.vram_budget_gb is None
    assert budget.constrained_by_live_usage is True
    assert budget.install_budget_gb == pytest.approx(19.2)


def test_idle_machine_is_still_capped_below_total_memory():
    budget = calculate_memory_budget(_hardware(ram_available_gb=24, vram_free_gb=24))

    assert budget.model_budget_gb == pytest.approx(19.2)
    assert budget.constrained_by_live_usage is False


def test_discrete_gpu_uses_live_free_vram_with_a_reserve():
    budget = calculate_memory_budget(
        _hardware(
            ram_total_gb=16,
            ram_available_gb=4,
            unified_memory=False,
            vram_total_gb=8,
            vram_free_gb=6,
        )
    )

    assert budget.ram_budget_gb == pytest.approx(2.4)
    assert budget.vram_budget_gb == pytest.approx(5.5)
    assert budget.model_budget_gb == pytest.approx(5.5)
    assert budget.install_budget_gb == pytest.approx(12.8)


def test_low_live_memory_never_becomes_negative():
    budget = calculate_memory_budget(_hardware(ram_available_gb=1, vram_free_gb=1))

    assert budget.model_budget_gb == 0


def test_model_fit_uses_installed_total_not_current_available_memory():
    """Install-time fit checks (search/install/recommend) shouldn't flap
    based on what else happens to be running on the machine right now."""
    hardware = _hardware(ram_available_gb=5, vram_free_gb=5)
    candidate = {"name": "model-7B-Q4", "size_bytes": 4 * 1024**3}

    assert predictor.available_model_memory_gb(hardware) == pytest.approx(19.2)
    assert predictor.candidate_fits_memory(hardware, candidate) is True
