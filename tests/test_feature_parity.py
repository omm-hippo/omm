from scripts import train_model

from omm.featurize import FEATURE_ORDER
from omm.hardware import HardwareInfo
from omm.predictor import build_prediction_features, estimate_required_memory_gb
from omm.tuning import RuntimeProfile


def _runtime(**overrides) -> RuntimeProfile:
    values = {
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
        "profile_name": "balanced",
        "model_size_gb": 4.0,
        "required_memory_gb": 4.8,
        "available_memory_gb": 12.0,
        "headroom_gb": 7.2,
        "quant_bits": 4.0,
    }
    values.update(overrides)
    return RuntimeProfile(**values)


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="Windows",
        os_version="11",
        cpu="AMD Ryzen 9 7950X3D",
        ram_total_gb=16,
        ram_available_gb=14,
        unified_memory=False,
        gpu_name="NVIDIA RTX 4090",
        vram_total_gb=8,
        vram_free_gb=7,
        gpu_tflops=20,
    )


def _row() -> dict:
    return {
        "engine": "ollama",
        "benchmark_version": 6,
        "tokens_per_sec": 20,
        "sample_count": 3,
        "tokens_per_sec_min": 20,
        "tokens_per_sec_max": 20,
        "ram_gb": 16,
        "vram_gb": 8,
        "gpu_tflops": 20,
        "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf",
        "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3,
        "parameter_count_b": 7.0,
        "active_parameter_count_b": 7.0,
        "quant_bits": 4.0,
        "engine_version": "0.12.0",
        "client_version": "0.1.44",
        "runtime_profile": "explicit_ollama_options",
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
        "cpu_model": "AMD Ryzen 9 7950X3D",
        "cpu_arch": "x86_64",
        "cpu_physical_cores": 16,
        "cpu_logical_cores": 32,
    }


def test_prediction_features_match_privacy_minimized_training_row():
    row = _row()
    training_sample, reason = train_model._real_row_to_sample(row)
    assert reason is None
    assert training_sample is not None
    training_features, _ = training_sample

    candidate = {
        "name": row["model_installed"],
        "filename": row["model_installed"],
        "repo_id": row["model_repo_id"],
        "size_bytes": row["model_size_bytes"],
    }
    assert build_prediction_features(_hardware(), candidate, runtime=_runtime()) == training_features


def test_runtime_fields_control_their_corresponding_prediction_features():
    candidate = {
        "name": "model-7B-Q4.gguf",
        "filename": "model-7B-Q4.gguf",
        "repo_id": "org/model-7B",
        "size_bytes": 4 * 1024**3,
    }
    baseline = build_prediction_features(_hardware(), candidate, runtime=_runtime())
    changed = build_prediction_features(
        _hardware(),
        candidate,
        runtime=_runtime(context_length=8192, gpu_offload_percent=50, cpu_threads=12, num_batch=256),
    )

    assert changed[FEATURE_ORDER.index("context_length")] == 8192
    assert changed[FEATURE_ORDER.index("gpu_offload_ratio")] == 0.5
    assert changed[FEATURE_ORDER.index("cpu_threads")] == 12
    assert changed[FEATURE_ORDER.index("num_batch")] == 256
    assert baseline != changed


def test_explicit_candidate_metadata_wins_and_sizes_unparseable_filename():
    candidate = {
        "name": "unparseable",
        "filename": "artifact.gguf",
        "repo_id": "org/also-unparseable",
        "parameter_count_b": 3.0,
        "active_parameter_count_b": 1.0,
        "quant_bits": 4.0,
    }

    features = build_prediction_features(_hardware(), candidate, runtime=_runtime())

    assert features[FEATURE_ORDER.index("param_count_b")] == 3.0
    assert features[FEATURE_ORDER.index("active_param_count_b")] == 1.0
    assert features[FEATURE_ORDER.index("quant_bits")] == 4.0
    assert features[FEATURE_ORDER.index("model_size_gb")] == 3.0 * 4.0 / 8.0 * 1.1
    assert estimate_required_memory_gb(candidate) == 3.0 * 4.0 / 8.0 * 1.1 * 1.2

    row = _row() | {
        "model_installed": "artifact.gguf",
        "model_repo_id": "org/also-unparseable",
        "model_size_bytes": None,
        "parameter_count_b": 3.0,
        "active_parameter_count_b": 1.0,
        "quant_bits": 4.0,
    }
    training_sample, reason = train_model._real_row_to_sample(row)
    assert reason is None
    assert training_sample is not None
    assert features == training_sample[0]


def test_explicit_metadata_beats_conflicting_filename_values():
    candidate = {
        "filename": "model-7B-Q4.gguf",
        "parameter_count_b": 3.0,
        "active_parameter_count_b": 1.0,
        "quant_bits": 8.0,
    }

    features = build_prediction_features(_hardware(), candidate, runtime=_runtime())

    assert features[FEATURE_ORDER.index("param_count_b")] == 3.0
    assert features[FEATURE_ORDER.index("active_param_count_b")] == 1.0
    assert features[FEATURE_ORDER.index("quant_bits")] == 8.0
    assert features[FEATURE_ORDER.index("model_size_gb")] == 3.0 * 8.0 / 8.0 * 1.1


def test_gpu_score_and_tier_are_derived_from_gpu_name_not_hardcoded_zero():
    hw = _hardware()  # gpu_name="NVIDIA RTX 4090" already set in this fixture
    candidate = {
        "name": "model-7B-Q4.gguf",
        "filename": "model-7B-Q4.gguf",
        "repo_id": "org/model-7B",
        "size_bytes": 4 * 1024**3,
    }
    features = build_prediction_features(hw, candidate, runtime=_runtime())
    gpu_score_index = FEATURE_ORDER.index("gpu_score")
    gpu_tier_index = FEATURE_ORDER.index("gpu_tier")
    assert features[gpu_score_index] == 4090.0
    assert features[gpu_tier_index] == 0.0
