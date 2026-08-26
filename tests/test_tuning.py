from __future__ import annotations

import pytest

from omm.hardware import HardwareInfo
from omm.tuning import candidate_model_size_gb, confidence_label, recommend_runtime_settings


def _hardware(**overrides) -> HardwareInfo:
    values = {
        "os_name": "Linux",
        "os_version": "",
        "cpu": "x86_64",
        "ram_total_gb": 16.0,
        "ram_available_gb": 12.0,
        "unified_memory": False,
        "gpu_name": None,
        "vram_total_gb": None,
        "vram_free_gb": None,
        "gpu_tflops": None,
    }
    values.update(overrides)
    return HardwareInfo(**values)


def test_cpu_profile_uses_safe_local_defaults():
    profile = recommend_runtime_settings(
        _hardware(), {"name": "model-7B-Q4", "size_bytes": 4 * 1024**3}, logical_cpu_count=32
    )

    assert profile.context_length == 4096
    assert profile.gpu_offload_percent == 0
    assert profile.cpu_threads == 16
    assert profile.num_batch == 128
    assert profile.ollama_options["num_gpu"] == 0


def test_unified_memory_profile_offloads_all_layers():
    profile = recommend_runtime_settings(
        _hardware(
            ram_total_gb=32,
            ram_available_gb=28,
            unified_memory=True,
            vram_total_gb=32,
            vram_free_gb=28,
        ),
        {"name": "model-7B-Q4", "size_bytes": 4 * 1024**3},
        logical_cpu_count=10,
    )

    assert profile.context_length == 8192
    assert profile.gpu_offload_percent == 100
    assert profile.num_batch == 512
    assert profile.ollama_options["num_gpu"] == -1


def test_busy_unified_machine_downgrades_context_but_keeps_full_offload():
    profile = recommend_runtime_settings(
        _hardware(
            ram_total_gb=24,
            ram_available_gb=5,
            unified_memory=True,
            vram_total_gb=24,
            vram_free_gb=5,
        ),
        {"name": "model-7B-Q4", "size_bytes": 4 * 1024**3},
        logical_cpu_count=10,
    )

    assert profile.available_memory_gb == pytest.approx(2.6)
    assert profile.context_length == 2048
    # RAM and VRAM are the same pool on unified memory, so offload is kept
    # at 100% even under memory pressure -- disabling it wouldn't free
    # anything, only slow inference down.
    assert profile.gpu_offload_percent == 100


def test_discrete_gpu_profile_recommends_partial_offload_when_needed():
    profile = recommend_runtime_settings(
        _hardware(vram_total_gb=4),
        {"name": "model-13B-Q4", "size_bytes": 8 * 1024**3},
        logical_cpu_count=8,
    )

    assert 10 <= profile.gpu_offload_percent < 100
    assert "num_gpu" not in profile.ollama_options


def test_confidence_label_tracks_real_evidence():
    assert confidence_label(3) == "experimental"
    assert confidence_label(50) == "growing"
    assert confidence_label(500) == "community measured"


def test_boolean_size_is_not_treated_as_one_byte_model():
    assert candidate_model_size_gb({"size_bytes": True, "name": "unknown"}) is None
