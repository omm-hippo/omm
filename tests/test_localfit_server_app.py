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
        outcome="success",
        tokens_per_sec=20.0,
        sample_count=3,
        tokens_per_sec_min=19.0,
        tokens_per_sec_max=21.0,
        parameter_count_b=7.0,
        active_parameter_count_b=7.0,
        quant_bits=4.0,
        engine_version="0.11.0",
        client_version="1.2.3",
        runtime_profile="explicit_ollama_options",
        context_length=4096,
        gpu_offload_percent=100,
        cpu_threads=8,
        num_batch=512,
        cpu_arch="arm64",
        cpu_physical_cores=10,
        cpu_logical_cores=10,
        cpu_score=7950.0,
        cpu_tier=1.0,
        gpu_score=4090.0,
        gpu_tier=0.0,
    )
    assert event.cpu_score == 7950.0
    assert event.gpu_tier == 0.0
