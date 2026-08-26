"""Profile-aware memory planning for ``omm contribute``.

Model weights, committed runtime memory, and dedicated VRAM are deliberately
kept separate.  A memory-mapped weight file is a working-set requirement, not
the same thing as anonymous committed RAM; collapsing both into ``size * 1.2``
made small models impossible to benchmark on otherwise usable 16 GiB PCs.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Sequence

from omm.hardware import HardwareInfo, WindowsCommitInfo

GIB = 1024**3
MIB = 1024**2

CONTRIBUTE_CONTEXT_LENGTH = 1024
CONTRIBUTE_NUM_BATCH = 128
KV_ELEMENT_BYTES = 2  # Ollama/llama.cpp default f16 K and V caches.

# This is an OS/application emergency floor, not model memory.  Volatility
# observed during sampling can raise it; it never contains model weights.
EMERGENCY_RESERVE_MIN_GB = 0.5
EMERGENCY_RESERVE_RATIO = 0.03
VRAM_RESERVE_MIN_GB = 0.25
VRAM_RESERVE_RATIO = 0.03

# Fixed-profile graph scratch approximation.  Unlike a blanket percentage of
# the GGUF, this scales with the two runtime inputs that actually affect it.
COMPUTE_BASE_BYTES = 64 * MIB
COMPUTE_ACTIVATION_COPIES = 32
RUNTIME_BASE_BYTES = 64 * MIB

FALLBACK_KV_BYTES = 64 * MIB
FALLBACK_COMPUTE_BYTES = 128 * MIB


class ContributionMemoryDecision(Enum):
    SAFE = "safe"
    DEFER = "defer"
    BLOCK = "block"


@dataclass(frozen=True)
class AvailableMemorySample:
    samples_gb: tuple[float, ...]
    median_gb: float
    minimum_gb: float
    maximum_gb: float
    reserve_gb: float


@dataclass(frozen=True)
class ContributionMemoryEstimate:
    mapped_weights_ram_gb: float
    committed_ram_gb: float
    required_vram_gb: float
    kv_cache_gb: float
    compute_buffer_gb: float
    runtime_overhead_gb: float
    source: str
    confidence: str
    context_length: int
    num_batch: int
    gpu_offload_percent: int
    # Host RAM that must stay resident for fixed-profile inference: KV cache,
    # compute buffers and runtime base, minus whatever the accelerator holds.
    # Model weights are deliberately excluded. Windows Ollama accounts them as
    # committed memory but can page them, so physical availability protects the
    # buffers while commit capacity protects the allocation. This is recorded at
    # estimate time rather than derived from committed_ram_gb, whose composition
    # varies with mmap and GPU offload.
    runtime_buffer_ram_gb: float

    @property
    def resident_ram_gb(self) -> float:
        return self.mapped_weights_ram_gb + self.committed_ram_gb


@dataclass(frozen=True)
class ContributionMemoryPlan:
    decision: ContributionMemoryDecision
    estimate: ContributionMemoryEstimate
    sample: AvailableMemorySample
    allocation_available_gb: float
    residency_available_gb: float
    commit_available_gb: float | None
    commit_limit_gb: float | None
    runtime_buffer_required_gb: float
    vram_available_gb: float | None
    reasons: tuple[str, ...]

    # Compatibility aliases for existing CLI diagnostics and integrations.
    @property
    def required_gb(self) -> float:
        return self.estimate.committed_ram_gb

    @property
    def available_gb(self) -> float:
        return self.allocation_available_gb

    @property
    def reserve_gb(self) -> float:
        return self.sample.reserve_gb


def weights_mmap_expected(hardware: HardwareInfo) -> bool:
    """Whether Ollama normally file-maps CPU-resident model weights.

    Current Windows Ollama explicitly disables mmap for CPU runner loads.
    Treat those weights as committed RAM. Other platforms keep the existing
    mmap assumption until their runtime reports otherwise.
    """
    return str(getattr(hardware, "os_name", "")).strip().casefold() != "windows"


def sample_available_memory(
    sample_available_gb: Callable[[], float],
    *,
    total_ram_gb: float,
    sample_count: int = 5,
    interval_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> AvailableMemorySample:
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 1 <= sample_count <= 20
    ):
        raise ValueError("sample_count must be between 1 and 20")
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or not 0 <= interval_seconds <= 5
    ):
        raise ValueError("interval_seconds must be between 0 and 5")
    if (
        isinstance(total_ram_gb, bool)
        or not isinstance(total_ram_gb, (int, float))
        or not math.isfinite(total_ram_gb)
        or total_ram_gb < 0
    ):
        raise ValueError("total_ram_gb must be finite and non-negative")
    values = []
    for index in range(sample_count):
        value = float(sample_available_gb())
        if not math.isfinite(value) or value < 0:
            raise ValueError("available memory sample must be finite and non-negative")
        values.append(value)
        if index + 1 < sample_count and interval_seconds:
            sleep(interval_seconds)
    median = statistics.median(values)
    minimum = min(values)
    volatility = max(0.0, median - minimum)
    reserve = max(
        EMERGENCY_RESERVE_MIN_GB,
        max(0.0, float(total_ram_gb)) * EMERGENCY_RESERVE_RATIO,
        volatility * 2.0,
    )
    return AvailableMemorySample(tuple(values), median, minimum, max(values), reserve)


def metadata_keys_for_architecture(architecture: str) -> set[str]:
    return {
        "general.architecture",
        f"{architecture}.block_count",
        f"{architecture}.embedding_length",
        f"{architecture}.attention.head_count",
        f"{architecture}.attention.head_count_kv",
        f"{architecture}.attention.key_length",
        f"{architecture}.attention.value_length",
    }


def _positive_number(metadata: Mapping[str, object], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _buffer_estimate_bytes(
    metadata: Mapping[str, object], context_length: int, num_batch: int
) -> tuple[float, float] | None:
    architecture = metadata.get("general.architecture")
    if not isinstance(architecture, str) or not architecture:
        return None
    layers = _positive_number(metadata, f"{architecture}.block_count")
    embedding = _positive_number(metadata, f"{architecture}.embedding_length")
    heads = _positive_number(metadata, f"{architecture}.attention.head_count")
    kv_heads = _positive_number(metadata, f"{architecture}.attention.head_count_kv") or heads
    if layers is None or embedding is None or heads is None or kv_heads is None:
        return None
    key_length = _positive_number(metadata, f"{architecture}.attention.key_length")
    value_length = _positive_number(metadata, f"{architecture}.attention.value_length")
    if key_length is None:
        key_length = embedding / heads
    if value_length is None:
        value_length = embedding / heads

    kv_bytes = (
        layers
        * kv_heads
        * (key_length + value_length)
        * context_length
        * KV_ELEMENT_BYTES
    )
    activation_width = max(embedding, kv_heads * (key_length + value_length))
    compute_bytes = (
        COMPUTE_BASE_BYTES
        + num_batch * activation_width * 4 * COMPUTE_ACTIVATION_COPIES
    )
    return kv_bytes, compute_bytes


def estimate_candidate_memory(
    candidate: Mapping[str, object],
    hardware: HardwareInfo,
    *,
    context_length: int,
    num_batch: int,
    gpu_offload_percent: int,
    metadata: Mapping[str, object] | None = None,
    mmap_weights: bool = True,
) -> ContributionMemoryEstimate | None:
    size_bytes = candidate.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, (int, float))
        or not math.isfinite(size_bytes)
        or size_bytes <= 0
    ):
        return None
    if (
        isinstance(context_length, bool)
        or not isinstance(context_length, int)
        or not 256 <= context_length <= 131_072
    ):
        raise ValueError("context_length must be between 256 and 131072")
    if (
        isinstance(num_batch, bool)
        or not isinstance(num_batch, int)
        or not 1 <= num_batch <= 65_536
    ):
        raise ValueError("num_batch must be between 1 and 65536")
    if (
        isinstance(gpu_offload_percent, bool)
        or not isinstance(gpu_offload_percent, int)
        or not 0 <= gpu_offload_percent <= 100
    ):
        raise ValueError("gpu_offload_percent must be between 0 and 100")

    weights_gb = float(size_bytes) / GIB
    buffers = _buffer_estimate_bytes(metadata or {}, context_length, num_batch)
    if buffers is None:
        kv_bytes = max(FALLBACK_KV_BYTES, size_bytes * 0.05 * context_length / 4096)
        compute_bytes = max(
            FALLBACK_COMPUTE_BYTES,
            size_bytes * 0.04 * num_batch / CONTRIBUTE_NUM_BATCH,
        )
        source, confidence = "profile_fallback", "low"
    else:
        kv_bytes, compute_bytes = buffers
        source, confidence = "gguf_header", "medium"
    runtime_bytes = RUNTIME_BASE_BYTES
    buffer_gb = (kv_bytes + compute_bytes) / GIB
    runtime_gb = runtime_bytes / GIB

    if hardware.unified_memory or hardware.vram_total_gb is None:
        mapped_ram = weights_gb if mmap_weights else 0.0
        committed_ram = buffer_gb + runtime_gb + (0.0 if mmap_weights else weights_gb)
        runtime_buffer_ram = buffer_gb + runtime_gb
        required_vram = 0.0
    else:
        gpu_fraction = gpu_offload_percent / 100.0
        cpu_weights = weights_gb * (1.0 - gpu_fraction)
        mapped_ram = cpu_weights if mmap_weights else 0.0
        gpu_weights = weights_gb * gpu_fraction
        # Ollama normally places KQV/compute on the accelerator when layers
        # are offloaded. Count the full buffers in VRAM for allocation safety;
        # retain the runtime base on RAM for host orchestration/staging.
        required_vram = gpu_weights + (buffer_gb if gpu_fraction > 0 else 0.0)
        host_buffer_gb = buffer_gb if gpu_fraction < 1 else 0.0
        committed_ram = (
            runtime_gb + host_buffer_gb + (0.0 if mmap_weights else cpu_weights)
        )
        runtime_buffer_ram = runtime_gb + host_buffer_gb

    return ContributionMemoryEstimate(
        mapped_weights_ram_gb=mapped_ram,
        committed_ram_gb=committed_ram,
        required_vram_gb=required_vram,
        kv_cache_gb=kv_bytes / GIB,
        compute_buffer_gb=compute_bytes / GIB,
        runtime_overhead_gb=runtime_gb,
        source=source,
        confidence=confidence,
        context_length=context_length,
        num_batch=num_batch,
        gpu_offload_percent=gpu_offload_percent,
        runtime_buffer_ram_gb=runtime_buffer_ram,
    )


def plan_candidate_memory(
    estimate: ContributionMemoryEstimate,
    hardware: HardwareInfo,
    sample: AvailableMemorySample,
    *,
    reclaimable_ram_gb: float = 0.0,
    reclaimable_vram_gb: float = 0.0,
    commit: WindowsCommitInfo | None = None,
) -> ContributionMemoryPlan:
    if commit is not None:
        values = (commit.available_gb, commit.limit_gb)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("commit counters must be finite and non-negative")
        if commit.available_gb > commit.limit_gb:
            raise ValueError("commit headroom cannot exceed the commit limit")
    physical_available = max(0.0, sample.median_gb - sample.reserve_gb) + max(
        0.0, reclaimable_ram_gb
    )
    is_windows = str(getattr(hardware, "os_name", "")).strip().casefold() == "windows"
    use_commit_headroom = is_windows and commit is not None
    allocation_available = (
        max(0.0, commit.available_gb - sample.reserve_gb) + max(0.0, reclaimable_ram_gb)
        if use_commit_headroom
        else physical_available
    )
    residency_available = physical_available
    vram_available = None
    reasons: list[str] = []

    total_ram_available = max(0.0, hardware.ram_total_gb - sample.reserve_gb)
    ram_allocation_fits_now = estimate.committed_ram_gb <= allocation_available
    # When Windows reports commit counters they are the authoritative allocation
    # budget, so the physical-capacity test would be the wrong denominator. Busy
    # headroom only defers; exceeding the whole commit limit blocks. A
    # system-managed pagefile can raise that limit later, but a candidate that
    # needs more than RAM plus the entire current pagefile would thrash long
    # before it produced a usable measurement.
    if use_commit_headroom:
        ram_allocation_possible = estimate.committed_ram_gb <= max(
            0.0, commit.limit_gb - sample.reserve_gb
        )
    else:
        ram_allocation_possible = estimate.committed_ram_gb <= total_ram_available
    if not ram_allocation_possible:
        reasons.append(
            "committed_ram_exceeds_commit_limit"
            if use_commit_headroom
            else "committed_ram_exceeds_physical_capacity"
        )
    elif not ram_allocation_fits_now:
        reasons.append("committed_ram_temporarily_unavailable")

    runtime_buffer_required = estimate.runtime_buffer_ram_gb
    runtime_buffers_fit_now = runtime_buffer_required <= physical_available
    runtime_buffers_possible = runtime_buffer_required <= total_ram_available
    if not runtime_buffers_possible:
        reasons.append("runtime_buffers_exceed_physical_capacity")
    elif not runtime_buffers_fit_now:
        reasons.append("runtime_buffers_temporarily_unavailable")

    vram_fits_now = True
    vram_possible = True
    if not hardware.unified_memory and hardware.vram_total_gb is not None:
        vram_free = hardware.vram_free_gb
        if vram_free is None:
            vram_free = hardware.vram_total_gb
        vram_reserve = max(VRAM_RESERVE_MIN_GB, hardware.vram_total_gb * VRAM_RESERVE_RATIO)
        vram_available = max(0.0, vram_free - vram_reserve) + max(0.0, reclaimable_vram_gb)
        vram_total_available = max(0.0, hardware.vram_total_gb - vram_reserve)
        vram_fits_now = estimate.required_vram_gb <= vram_available
        vram_possible = estimate.required_vram_gb <= vram_total_available
        if not vram_possible:
            reasons.append("vram_exceeds_physical_capacity")
        elif not vram_fits_now:
            reasons.append("vram_temporarily_unavailable")

    if not ram_allocation_possible or not runtime_buffers_possible or not vram_possible:
        decision = ContributionMemoryDecision.BLOCK
    elif not ram_allocation_fits_now or not runtime_buffers_fit_now or not vram_fits_now:
        decision = ContributionMemoryDecision.DEFER
    else:
        # Only estimate.mapped_weights_ram_gb is excluded from the allocation
        # gate. Non-mmap CPU weights are already part of committed_ram_gb.
        # Runtime samples and speed dispersion still classify paging noise.
        decision = ContributionMemoryDecision.SAFE

    return ContributionMemoryPlan(
        decision=decision,
        estimate=estimate,
        sample=sample,
        allocation_available_gb=allocation_available,
        residency_available_gb=residency_available,
        commit_available_gb=commit.available_gb if use_commit_headroom else None,
        commit_limit_gb=commit.limit_gb if use_commit_headroom else None,
        runtime_buffer_required_gb=runtime_buffer_required,
        vram_available_gb=vram_available,
        reasons=tuple(reasons),
    )


def speed_mad_ratio(samples: Sequence[float]) -> float:
    values = [float(value) for value in samples if math.isfinite(float(value)) and value >= 0]
    if not values:
        return 0.0
    median = statistics.median(values)
    if median <= 0:
        return 0.0
    return statistics.median(abs(value - median) for value in values) / median
