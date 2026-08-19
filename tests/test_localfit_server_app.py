"""Tests for localfit_server.app models."""

import pytest

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


def test_benchmark_event_accepts_clean_v9_contribution_metadata():
    event = BenchmarkEvent(
        ram_gb=16,
        unified_memory=False,
        model_installed="small:latest",
        engine="ollama",
        benchmark_version=9,
        recorded_at="2026-08-18T00:00:00+00:00",
        outcome="success",
        tokens_per_sec=20.0,
        sample_count=3,
        tokens_per_sec_min=19.0,
        tokens_per_sec_max=21.0,
        parameter_count_b=1.0,
        active_parameter_count_b=1.0,
        quant_bits=4.0,
        engine_version="0.11.0",
        client_version="1.2.3",
        runtime_profile="explicit_ollama_options",
        context_length=1024,
        gpu_offload_percent=0,
        cpu_threads=8,
        num_batch=128,
        cpu_arch="x86_64",
        cpu_physical_cores=4,
        cpu_logical_cores=8,
        cpu_score=5600.0,
        cpu_tier=3.0,
        measurement_profile="contribute-v1",
        measurement_quality="clean",
        ram_available_before_gb=2.2,
        ram_available_min_gb=2.0,
        ram_available_after_gb=2.1,
        memory_pressure_observed=False,
        tokens_per_sec_mad_ratio=0.02,
        memory_estimate_source="gguf_header",
        memory_estimate_confidence="medium",
        estimated_mapped_weights_gb=0.75,
        estimated_committed_ram_gb=0.3,
        estimated_required_vram_gb=0.0,
    )

    assert event.benchmark_version == 9
    assert event.measurement_quality == "clean"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"context_length": 4096}, "fixed"),
        (
            {
                "measurement_quality": "pressured",
                "memory_pressure_observed": False,
            },
            "observed pressure",
        ),
        (
            {
                "measurement_quality": "clean",
                "tokens_per_sec_mad_ratio": 0.2,
            },
            "MAD ratio",
        ),
        # `loaded` is the one label that rests on a field older clients never
        # send, so an unsupported claim to it must not validate.
        ({"measurement_quality": "loaded"}, "require host_cpu_load_percent"),
        (
            {"measurement_quality": "loaded", "host_cpu_load_percent": 4.0},
            "must be clean",
        ),
        ({"host_cpu_load_percent": 61.0}, "must be loaded"),
        # Precedence: pressured > unstable > loaded > clean.
        (
            {
                "measurement_quality": "loaded",
                "host_cpu_load_percent": 61.0,
                "tokens_per_sec_mad_ratio": 0.4,
            },
            "must be unstable",
        ),
        (
            {
                "measurement_quality": "loaded",
                "host_cpu_load_percent": 61.0,
                "memory_pressure_observed": True,
            },
            "labelled pressured",
        ),
        ({"host_cpu_load_percent": 140.0}, "less than or equal to 100"),
        ({"host_cpu_load_percent": -1.0}, "greater than or equal to 0"),
    ],
)
def test_benchmark_event_rejects_inconsistent_v9_metadata(changes, message):
    payload = {
        "ram_gb": 16,
        "unified_memory": False,
        "model_installed": "small:latest",
        "engine": "ollama",
        "benchmark_version": 9,
        "recorded_at": "2026-08-18T00:00:00+00:00",
        "outcome": "success",
        "tokens_per_sec": 20.0,
        "sample_count": 3,
        "tokens_per_sec_min": 19.0,
        "tokens_per_sec_max": 21.0,
        "parameter_count_b": 1.0,
        "active_parameter_count_b": 1.0,
        "quant_bits": 4.0,
        "engine_version": "0.11.0",
        "client_version": "1.2.3",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 1024,
        "gpu_offload_percent": 0,
        "cpu_threads": 8,
        "num_batch": 128,
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 4,
        "cpu_logical_cores": 8,
        "cpu_score": 5600.0,
        "cpu_tier": 3.0,
        "measurement_profile": "contribute-v1",
        "measurement_quality": "clean",
        "ram_available_before_gb": 2.2,
        "ram_available_min_gb": 2.0,
        "ram_available_after_gb": 2.1,
        "memory_pressure_observed": False,
        "tokens_per_sec_mad_ratio": 0.02,
        "memory_estimate_source": "gguf_header",
        "memory_estimate_confidence": "medium",
        "estimated_mapped_weights_gb": 0.75,
        "estimated_committed_ram_gb": 0.3,
        "estimated_required_vram_gb": 0.0,
    }
    payload.update(changes)

    with pytest.raises(ValueError, match=message):
        BenchmarkEvent(**payload)


def _v9_success(**changes):
    payload = {
        "ram_gb": 16,
        "unified_memory": False,
        "model_installed": "small:latest",
        "engine": "ollama",
        "benchmark_version": 9,
        "recorded_at": "2026-08-18T00:00:00+00:00",
        "outcome": "success",
        "tokens_per_sec": 20.0,
        "sample_count": 3,
        "tokens_per_sec_min": 19.0,
        "tokens_per_sec_max": 21.0,
        "parameter_count_b": 1.0,
        "active_parameter_count_b": 1.0,
        "quant_bits": 4.0,
        "engine_version": "0.11.0",
        "client_version": "1.2.3",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 1024,
        "gpu_offload_percent": 0,
        "cpu_threads": 8,
        "num_batch": 128,
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 4,
        "cpu_logical_cores": 8,
        "cpu_score": 5600.0,
        "cpu_tier": 3.0,
        "measurement_profile": "contribute-v1",
        "measurement_quality": "clean",
        "ram_available_before_gb": 2.2,
        "ram_available_min_gb": 2.0,
        "ram_available_after_gb": 2.1,
        "memory_pressure_observed": False,
        "tokens_per_sec_mad_ratio": 0.02,
        "memory_estimate_source": "gguf_header",
        "memory_estimate_confidence": "medium",
        "estimated_mapped_weights_gb": 0.75,
        "estimated_committed_ram_gb": 0.3,
        "estimated_required_vram_gb": 0.0,
    }
    payload.update(changes)
    return BenchmarkEvent(**payload)


def test_benchmark_event_accepts_a_load_labelled_v9_row():
    event = _v9_success(measurement_quality="loaded", host_cpu_load_percent=61.0)

    assert event.measurement_quality == "loaded"
    assert event.host_cpu_load_percent == 61.0


def test_benchmark_event_labels_the_threshold_itself_loaded():
    """The boundary must land the same way here, in the Firebase rules, and
    in the client's own BUSY_CPU_PERCENT check, which is `>=`."""
    from omm.hardware import BUSY_CPU_PERCENT

    event = _v9_success(
        measurement_quality="loaded", host_cpu_load_percent=BUSY_CPU_PERCENT
    )

    assert event.host_cpu_load_percent == BUSY_CPU_PERCENT


def test_benchmark_event_accepts_a_quiet_host_reading_on_a_clean_row():
    event = _v9_success(host_cpu_load_percent=3.2)

    assert event.measurement_quality == "clean"


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"measurement_quality": "pressured", "memory_pressure_observed": True},
        {"measurement_quality": "unstable", "tokens_per_sec_mad_ratio": 0.2},
    ],
)
def test_benchmark_event_still_accepts_v9_rows_without_a_host_reading(changes):
    """Regression pin for clients in the wild: the three original labels are
    judged by the two original signals when no reading is present."""
    event = _v9_success(**changes)

    assert event.host_cpu_load_percent is None


@pytest.mark.parametrize(
    ("quality", "changes"),
    [
        ("pressured", {"memory_pressure_observed": True}),
        ("unstable", {"tokens_per_sec_mad_ratio": 0.2}),
    ],
)
def test_benchmark_event_keeps_the_older_signals_ahead_of_host_load(quality, changes):
    event = _v9_success(
        measurement_quality=quality, host_cpu_load_percent=80.0, **changes
    )

    assert event.measurement_quality == quality
    assert event.host_cpu_load_percent == 80.0


def _lmstudio_v8_success(**changes):
    fields = {
        "ram_gb": 15.5,
        "unified_memory": False,
        "model_installed": "qwen2.5-0.5b-instruct",
        "engine": "lmstudio",
        "benchmark_version": 8,
        "recorded_at": "2026-08-18T00:00:00+00:00",
        "outcome": "success",
        "tokens_per_sec": 103.2,
        "sample_count": 3,
        "tokens_per_sec_min": 100.0,
        "tokens_per_sec_max": 106.0,
        "parameter_count_b": 0.5,
        "active_parameter_count_b": 0.5,
        "quant_bits": 4.0,
        "engine_version": "0.4.21",
        "client_version": "0.2.95",
        "cpu_arch": "AMD64",
        "cpu_physical_cores": 16,
        "cpu_logical_cores": 22,
        "cpu_score": 0.0,
        "cpu_tier": 3.0,
    }
    fields.update(changes)
    return BenchmarkEvent(**fields)


def test_benchmark_event_accepts_an_lmstudio_v8_row_without_runtime_metadata():
    event = _lmstudio_v8_success()

    assert event.engine == "lmstudio"
    assert event.runtime_profile is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("runtime_profile", "explicit_ollama_options"),
        ("context_length", 4096),
        ("gpu_offload_percent", 100),
        ("cpu_threads", 8),
        ("num_batch", 512),
    ],
)
def test_benchmark_event_rejects_fabricated_lmstudio_runtime_metadata(field, value):
    with pytest.raises(ValueError, match="must not include runtime metadata"):
        _lmstudio_v8_success(**{field: value})


def test_benchmark_event_still_requires_runtime_metadata_for_ollama_v8():
    with pytest.raises(ValueError, match="requires runtime metadata"):
        _lmstudio_v8_success(engine="ollama")


def test_benchmark_event_rejects_an_lmstudio_row_claiming_the_v9_profile():
    with pytest.raises(ValueError, match="require the ollama engine"):
        _lmstudio_v8_success(benchmark_version=9)


def test_benchmark_event_still_requires_an_engine_version_from_lmstudio():
    with pytest.raises(ValueError, match="requires model metadata"):
        _lmstudio_v8_success(engine_version=None)
