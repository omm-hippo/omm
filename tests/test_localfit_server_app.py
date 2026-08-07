"""Tests for localfit_server.app models."""

from localfit_server.app import BenchmarkEvent


def test_benchmark_event_accepts_v8_chip_score_fields():
    event = BenchmarkEvent(
        ram_gb=16,
        unified_memory=False,
        model_installed="small:latest",
        engine="ollama",
        benchmark_version=8,
        recorded_at="2026-07-30T00:00:00+00:00",
        tokens_per_sec=20.0,
        cpu_score=7950.0,
        cpu_tier=1.0,
        gpu_score=4090.0,
        gpu_tier=0.0,
    )
    assert event.cpu_score == 7950.0
    assert event.gpu_tier == 0.0
