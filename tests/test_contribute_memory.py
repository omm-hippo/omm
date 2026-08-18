import struct

import pytest

from omm import cli, contribute_memory
from omm.gguf import read_gguf_metadata_bytes
from omm.hardware import HardwareInfo, WindowsCommitInfo
from omm.tuning import contribute_ollama_options, recommend_contribute_settings


def _hardware(**overrides):
    values = {
        "os_name": "Windows",
        "os_version": "11",
        "cpu": "CPU",
        "ram_total_gb": 15.5,
        "ram_available_gb": 2.2,
        "unified_memory": False,
        "gpu_name": None,
        "vram_total_gb": None,
        "vram_free_gb": None,
    }
    values.update(overrides)
    return HardwareInfo(**values)


def _commit(available_gb, limit_gb=32.0):
    """Windows commit counters with headroom pinned for determinism.

    The default limit is far above every candidate in these tests, so only
    ``available_gb`` decides SAFE vs DEFER unless a test says otherwise.
    """
    return WindowsCommitInfo(available_gb=available_gb, limit_gb=limit_gb)


def _gguf_scalar(key, value_type, payload):
    encoded = key.encode()
    return struct.pack("<Q", len(encoded)) + encoded + struct.pack("<I", value_type) + payload


def _gguf_bytes(entries):
    return b"GGUF" + struct.pack("<IQQ", 3, 0, len(entries)) + b"".join(entries)


def _llama_metadata():
    return {
        "general.architecture": "llama",
        "llama.block_count": 32,
        "llama.embedding_length": 4096,
        "llama.attention.head_count": 32,
        "llama.attention.head_count_kv": 8,
    }


def test_gguf_reader_returns_typed_scalars_and_rejects_short_prefix():
    data = _gguf_bytes(
        [
            _gguf_scalar("general.architecture", 8, struct.pack("<Q", 5) + b"llama"),
            _gguf_scalar("llama.block_count", 4, struct.pack("<I", 32)),
            _gguf_scalar("feature.enabled", 7, struct.pack("<B", 1)),
        ]
    )

    result = read_gguf_metadata_bytes(
        data,
        {"general.architecture", "llama.block_count", "feature.enabled"},
    )

    assert result == {
        "general.architecture": "llama",
        "llama.block_count": 32,
        "feature.enabled": True,
    }
    with pytest.raises(struct.error):
        read_gguf_metadata_bytes(data[:-1], {"feature.enabled"})


def test_fixed_contribute_profile_is_independent_of_live_headroom():
    candidate = {"filename": "model-1B-Q4.gguf", "size_bytes": int(0.75 * 1024**3)}

    low = recommend_contribute_settings(_hardware(ram_available_gb=2.2), candidate)
    high = recommend_contribute_settings(_hardware(ram_available_gb=14.0), candidate)

    assert low.context_length == high.context_length == 1024
    assert low.num_batch == high.num_batch == 128
    assert low.profile_name == high.profile_name == "contribute-v1"


def test_contribute_gpu_placement_uses_total_not_momentary_free_vram():
    candidate = {"filename": "model-3B-Q4.gguf", "size_bytes": 2 * 1024**3}
    busy = recommend_contribute_settings(
        _hardware(gpu_name="GPU", vram_total_gb=8.0, vram_free_gb=0.2),
        candidate,
    )
    idle = recommend_contribute_settings(
        _hardware(gpu_name="GPU", vram_total_gb=8.0, vram_free_gb=7.5),
        candidate,
    )

    assert busy.gpu_offload_percent == idle.gpu_offload_percent == 100


def test_partial_contribute_offload_becomes_an_explicit_layer_count():
    candidate = {"filename": "model-13B-Q4.gguf", "size_bytes": 8 * 1024**3}
    profile = recommend_contribute_settings(
        _hardware(gpu_name="GPU", vram_total_gb=4.0, vram_free_gb=4.0),
        candidate,
    )
    assert 0 < profile.gpu_offload_percent < 100

    options, actual_percent = contribute_ollama_options(profile, _llama_metadata())

    assert options["num_gpu"] > 0
    assert actual_percent == round(100 * options["num_gpu"] / 32)


def test_partial_contribute_offload_without_layer_metadata_falls_back_to_cpu():
    candidate = {"filename": "model-13B-Q4.gguf", "size_bytes": 8 * 1024**3}
    profile = recommend_contribute_settings(
        _hardware(gpu_name="GPU", vram_total_gb=4.0, vram_free_gb=4.0),
        candidate,
    )

    options, actual_percent = contribute_ollama_options(profile, {})

    assert options["num_gpu"] == 0
    assert actual_percent == 0


def test_header_estimate_separates_mapped_weights_from_committed_buffers():
    candidate = {"size_bytes": int(0.75 * 1024**3)}

    estimate = contribute_memory.estimate_candidate_memory(
        candidate,
        _hardware(),
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
    )

    assert estimate is not None
    assert estimate.source == "gguf_header"
    assert estimate.mapped_weights_ram_gb == pytest.approx(0.75)
    assert estimate.kv_cache_gb == pytest.approx(0.125)
    assert estimate.compute_buffer_gb == pytest.approx(0.125)
    assert estimate.committed_ram_gb == pytest.approx(0.3125)


def test_windows_cpu_ollama_counts_non_mmap_weights_as_committed_ram():
    hardware = _hardware()

    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(0.75 * 1024**3)},
        hardware,
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
        mmap_weights=contribute_memory.weights_mmap_expected(hardware),
    )

    assert estimate is not None
    assert contribute_memory.weights_mmap_expected(hardware) is False
    assert estimate.mapped_weights_ram_gb == 0
    assert estimate.committed_ram_gb == pytest.approx(1.0625)
    assert contribute_memory.weights_mmap_expected(_hardware(os_name="Linux")) is True


def test_windows_non_mmap_small_model_still_passes_with_observed_2_72gb_available():
    hardware = _hardware(ram_available_gb=2.72)
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(0.75 * 1024**3)},
        hardware,
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
        mmap_weights=False,
    )
    sample = contribute_memory.AvailableMemorySample(
        samples_gb=(2.7, 2.72, 2.74),
        median_gb=2.72,
        minimum_gb=2.7,
        maximum_gb=2.74,
        reserve_gb=0.5,
    )

    plan = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(8.0)
    )

    assert plan.decision is contribute_memory.ContributionMemoryDecision.SAFE
    assert estimate.committed_ram_gb == pytest.approx(1.0625)


def test_windows_uses_commit_headroom_but_physical_gate_only_covers_runtime_buffers():
    hardware = _hardware(ram_available_gb=1.14)
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(0.75 * 1024**3)},
        hardware,
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
        mmap_weights=False,
    )
    sample = contribute_memory.AvailableMemorySample((1.1, 1.14, 1.18), 1.14, 1.1, 1.18, 0.5)

    plan = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(7.0)
    )

    assert estimate.committed_ram_gb > 1.0
    assert plan.runtime_buffer_required_gb == pytest.approx(0.3125)
    assert plan.residency_available_gb == pytest.approx(0.64)
    assert plan.decision is contribute_memory.ContributionMemoryDecision.SAFE


def test_windows_defers_when_commit_headroom_is_insufficient_even_if_physical_ram_is_free():
    hardware = _hardware(ram_available_gb=8.0)
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(0.75 * 1024**3)}, hardware,
        context_length=1024, num_batch=128, gpu_offload_percent=0,
        metadata=_llama_metadata(), mmap_weights=False,
    )
    sample = contribute_memory.AvailableMemorySample((8.0,), 8.0, 8.0, 8.0, 0.5)

    plan = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(1.2)
    )

    assert plan.decision is contribute_memory.ContributionMemoryDecision.DEFER
    assert "committed_ram_temporarily_unavailable" in plan.reasons


def test_windows_blocks_only_when_the_whole_commit_limit_is_too_small():
    """Busy headroom defers; a candidate larger than the limit itself blocks.

    A system-managed pagefile can raise CommitLimit later, so the block is
    reserved for candidates that exceed RAM plus the entire current pagefile -
    a load that would thrash long before it produced a usable measurement.
    """
    hardware = _hardware(ram_available_gb=8.0)
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(6.0 * 1024**3)},
        hardware,
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
        mmap_weights=False,
    )
    sample = contribute_memory.AvailableMemorySample((8.0,), 8.0, 8.0, 8.0, 0.5)

    busy = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(1.0, limit_gb=32.0)
    )
    too_small = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(1.0, limit_gb=4.0)
    )

    assert busy.decision is contribute_memory.ContributionMemoryDecision.DEFER
    assert "committed_ram_temporarily_unavailable" in busy.reasons
    assert busy.commit_limit_gb == 32.0
    assert too_small.decision is contribute_memory.ContributionMemoryDecision.BLOCK
    assert "committed_ram_exceeds_commit_limit" in too_small.reasons


def test_commit_limit_does_not_block_when_physical_capacity_still_governs():
    """Off Windows, and with no commit counters, the portable gate is unchanged."""
    hardware = _hardware(os_name="Darwin", ram_total_gb=15.5, ram_available_gb=8.0)
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(6.0 * 1024**3)},
        hardware,
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
    )
    sample = contribute_memory.AvailableMemorySample((8.0,), 8.0, 8.0, 8.0, 0.5)

    plan = contribute_memory.plan_candidate_memory(
        estimate, hardware, sample, commit=_commit(0.1, limit_gb=0.2)
    )

    assert plan.commit_available_gb is None
    assert plan.commit_limit_gb is None
    assert plan.decision is contribute_memory.ContributionMemoryDecision.SAFE


def test_runtime_buffers_exclude_whatever_the_accelerator_holds():
    """The recorded host buffer must not be re-derived from committed RAM.

    Full offload moves KV/compute to VRAM, so only the runtime base stays on
    the host - a distinction committed_ram_gb alone cannot express once mmap
    and partial offload change its composition.
    """
    hardware = _hardware(vram_total_gb=24.0, vram_free_gb=24.0)
    candidate = {"size_bytes": int(4.0 * 1024**3)}
    common = dict(
        context_length=1024, num_batch=128, metadata=_llama_metadata(), mmap_weights=False
    )

    cpu_only = contribute_memory.estimate_candidate_memory(
        candidate, hardware, gpu_offload_percent=0, **common
    )
    full_offload = contribute_memory.estimate_candidate_memory(
        candidate, hardware, gpu_offload_percent=100, **common
    )

    buffers = cpu_only.kv_cache_gb + cpu_only.compute_buffer_gb
    assert cpu_only.runtime_buffer_ram_gb == pytest.approx(
        buffers + cpu_only.runtime_overhead_gb
    )
    assert full_offload.runtime_buffer_ram_gb == pytest.approx(
        full_offload.runtime_overhead_gb
    )
    assert full_offload.runtime_buffer_ram_gb < cpu_only.runtime_buffer_ram_gb


def test_16gb_regression_small_model_passes_with_2_2gb_available():
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": int(0.75 * 1024**3)},
        _hardware(),
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        metadata=_llama_metadata(),
    )
    sample = contribute_memory.AvailableMemorySample(
        samples_gb=(2.1, 2.2, 2.3),
        median_gb=2.2,
        minimum_gb=2.1,
        maximum_gb=2.3,
        reserve_gb=0.5,
    )

    plan = contribute_memory.plan_candidate_memory(estimate, _hardware(), sample)

    assert plan.decision is contribute_memory.ContributionMemoryDecision.SAFE
    assert plan.allocation_available_gb == pytest.approx(1.7)


def test_live_catalog_candidate_without_size_uses_remote_size_before_download(monkeypatch):
    candidate = {
        "provider": "huggingface",
        "repo_id": "org/repo",
        "filename": "model-1B-Q4.gguf",
    }
    sample = contribute_memory.AvailableMemorySample(
        samples_gb=(1.2,), median_gb=1.2, minimum_gb=1.2, maximum_gb=1.2, reserve_gb=0.5
    )
    size_calls = []
    monkeypatch.setattr(
        cli,
        "remote_file_size",
        lambda provider, repo_id, filename: size_calls.append((provider, repo_id, filename))
        or int(0.75 * 1024**3),
    )
    monkeypatch.setattr(cli, "remote_gguf_metadata", lambda *args, **kwargs: _llama_metadata())
    monkeypatch.setattr(
        cli.memory_guard_mod.OllamaManagedRuntime, "list_residents", lambda self: ()
    )

    plan = cli._contribute_candidate_memory_plan(
        candidate,
        hw=_hardware(ram_available_gb=1.2),
        memory_sample=sample,
        # Pinned: the Windows planner otherwise reads this machine's live
        # commit headroom, which would make the decision assertion below
        # depend on whatever else the developer has open.
        commit=_commit(0.8),
    )

    assert plan is not None
    assert size_calls == [("huggingface", "org/repo", "model-1B-Q4.gguf")]
    assert plan.estimate.mapped_weights_ram_gb == 0
    assert plan.estimate.committed_ram_gb == pytest.approx(1.0625)
    assert plan.decision is contribute_memory.ContributionMemoryDecision.DEFER


def test_catalog_preflight_without_network_uses_name_size_estimate(monkeypatch):
    candidate = {
        "provider": "huggingface",
        "repo_id": "org/repo",
        "filename": "model-1B-Q4.gguf",
    }
    sample = contribute_memory.AvailableMemorySample(
        samples_gb=(1.2,), median_gb=1.2, minimum_gb=1.2, maximum_gb=1.2, reserve_gb=0.5
    )
    monkeypatch.setattr(
        cli.memory_guard_mod.OllamaManagedRuntime, "list_residents", lambda self: ()
    )

    plan = cli._contribute_candidate_memory_plan(
        candidate,
        hw=_hardware(ram_available_gb=1.2),
        memory_sample=sample,
        fetch_remote_metadata=False,
    )

    assert plan is not None
    assert plan.estimate.mapped_weights_ram_gb == 0
    assert plan.estimate.committed_ram_gb > 0.55
    assert plan.estimate.source == "profile_fallback"


def test_live_memory_sampling_does_not_repeat_full_hardware_scan(monkeypatch):
    candidate = {"filename": "model-1B-Q4.gguf"}
    samples = []
    monkeypatch.setattr(
        cli,
        "scan_hardware",
        lambda: (_ for _ in ()).throw(AssertionError("must not rescan hardware")),
    )
    monkeypatch.setattr(
        cli,
        "available_ram_gb",
        lambda: samples.append(1.2) or 1.2,
    )

    plan = cli._contribute_candidate_memory_plan(
        candidate,
        hw=_hardware(ram_available_gb=1.2),
        residents=(),
        fetch_remote_metadata=False,
    )

    assert plan is not None
    assert samples == [1.2] * 5


def test_plan_allows_mapped_weights_and_defers_transient_committed_allocation():
    sample = contribute_memory.AvailableMemorySample(
        samples_gb=(2.2,), median_gb=2.2, minimum_gb=2.2, maximum_gb=2.2, reserve_gb=0.5
    )
    base = contribute_memory.ContributionMemoryEstimate(
        mapped_weights_ram_gb=2.0,
        committed_ram_gb=0.3,
        required_vram_gb=0.0,
        kv_cache_gb=0.1,
        compute_buffer_gb=0.1,
        runtime_overhead_gb=0.1,
        source="gguf_header",
        confidence="medium",
        context_length=1024,
        num_batch=128,
        gpu_offload_percent=0,
        runtime_buffer_ram_gb=0.3,
    )
    mapped = contribute_memory.plan_candidate_memory(base, _hardware(), sample)
    deferred = contribute_memory.plan_candidate_memory(
        contribute_memory.ContributionMemoryEstimate(
            **{**base.__dict__, "mapped_weights_ram_gb": 0.1, "committed_ram_gb": 1.8}
        ),
        _hardware(),
        sample,
    )
    blocked = contribute_memory.plan_candidate_memory(
        contribute_memory.ContributionMemoryEstimate(
            **{**base.__dict__, "mapped_weights_ram_gb": 0.1, "committed_ram_gb": 20.0}
        ),
        _hardware(),
        sample,
    )

    assert mapped.decision is contribute_memory.ContributionMemoryDecision.SAFE
    assert mapped.reasons == ()
    assert deferred.decision is contribute_memory.ContributionMemoryDecision.DEFER
    assert "committed_ram_temporarily_unavailable" in deferred.reasons
    assert blocked.decision is contribute_memory.ContributionMemoryDecision.BLOCK
    assert "committed_ram_exceeds_physical_capacity" in blocked.reasons


def test_available_sampling_uses_median_and_observed_volatility():
    values = iter([2.2, 9.0, 2.1, 2.3, 2.2])
    sample = contribute_memory.sample_available_memory(
        lambda: next(values),
        total_ram_gb=15.5,
        interval_seconds=0,
    )

    assert sample.median_gb == pytest.approx(2.2)
    assert sample.minimum_gb == pytest.approx(2.1)
    assert sample.reserve_gb == pytest.approx(0.5)


def test_speed_mad_ratio_is_robust_to_one_outlier():
    assert contribute_memory.speed_mad_ratio([100.0, 101.0, 160.0]) == pytest.approx(
        1 / 101
    )
