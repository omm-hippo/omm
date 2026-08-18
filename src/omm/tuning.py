"""Conservative runtime-setting recommendations for local GGUF inference.

The goal is to provide a safe starting point, not pretend that one exact set
of flags is optimal for every backend.  Values are derived from information
Localfit can verify: model size, usable RAM/VRAM, unified memory, and CPU count.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Mapping

from omm.contribute_memory import (
    CONTRIBUTE_CONTEXT_LENGTH,
    CONTRIBUTE_NUM_BATCH,
    VRAM_RESERVE_MIN_GB,
    VRAM_RESERVE_RATIO,
)

from omm.featurize import parse_param_count_billions, parse_quant_bits
from omm.hardware import HardwareInfo, calculate_memory_budget

MEMORY_OVERHEAD = 1.2


@dataclass(frozen=True)
class RuntimeProfile:
    context_length: int
    gpu_offload_percent: int
    cpu_threads: int
    num_batch: int
    profile_name: str
    model_size_gb: float | None
    required_memory_gb: float | None
    available_memory_gb: float
    headroom_gb: float | None
    quant_bits: float | None

    @property
    def gpu_offload_label(self) -> str:
        if self.gpu_offload_percent >= 100:
            return "all layers"
        if self.gpu_offload_percent <= 0:
            return "CPU only"
        return f"about {self.gpu_offload_percent}%"

    @property
    def ollama_options(self) -> dict[str, int]:
        options = {
            "num_ctx": self.context_length,
            "num_thread": self.cpu_threads,
            "num_batch": self.num_batch,
        }
        if self.gpu_offload_percent >= 100:
            options["num_gpu"] = -1
        elif self.gpu_offload_percent <= 0:
            options["num_gpu"] = 0
        return options


def _candidate_text(candidate: dict) -> str:
    return (
        f"{candidate.get('name', '')} "
        f"{candidate.get('filename', '')} "
        f"{candidate.get('repo_id', '')}"
    )


def candidate_model_size_gb(candidate: dict) -> float | None:
    size_bytes = candidate.get("size_bytes")
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        return float(size_bytes) / (1024**3)
    text = _candidate_text(candidate)
    parameters = parse_param_count_billions(text)
    quant_bits = parse_quant_bits(text)
    if parameters is None or quant_bits is None:
        return None
    return parameters * quant_bits / 8.0 * 1.1


def candidate_quant_bits(candidate: dict) -> float | None:
    for value in (
        candidate.get("filename"),
        candidate.get("repo_id"),
        candidate.get("name"),
    ):
        parsed = parse_quant_bits(str(value or ""))
        if parsed is not None:
            return parsed
    return None


def available_model_memory_gb(hw: HardwareInfo) -> float:
    return calculate_memory_budget(hw).model_budget_gb


def recommend_runtime_settings(
    hw: HardwareInfo,
    candidate: dict,
    logical_cpu_count: int | None = None,
) -> RuntimeProfile:
    model_size = candidate_model_size_gb(candidate)
    required = model_size * MEMORY_OVERHEAD if model_size is not None else None
    available = available_model_memory_gb(hw)
    headroom = available - required if required is not None else None

    if headroom is not None and headroom >= 8.0:
        context_length, profile_name = 8192, "quality"
    elif headroom is not None and headroom >= 2.0:
        context_length, profile_name = 4096, "balanced"
    else:
        context_length, profile_name = 2048, "safe"

    memory_budget = calculate_memory_budget(hw)
    vram = memory_budget.vram_budget_gb or 0.0
    if hw.unified_memory:
        # RAM and VRAM are the same physical pool on unified memory, so
        # disabling GPU offload never frees memory the model needs -- it
        # only makes inference slower. Always prefer full offload here;
        # low-headroom machines are protected via context_length instead.
        gpu_offload_percent = 100
    elif vram <= 0 or model_size is None:
        gpu_offload_percent = 0
    elif model_size <= vram * 0.85:
        gpu_offload_percent = 100
    else:
        gpu_offload_percent = max(10, min(90, round((vram * 0.8 / model_size) * 100)))

    detected_threads = logical_cpu_count if logical_cpu_count is not None else os.cpu_count()
    cpu_threads = max(1, min(int(detected_threads or 4), 16))
    if gpu_offload_percent >= 80 and (headroom or 0.0) >= 2.0:
        num_batch = 512
    elif gpu_offload_percent > 0:
        num_batch = 256
    else:
        num_batch = 128

    return RuntimeProfile(
        context_length=context_length,
        gpu_offload_percent=gpu_offload_percent,
        cpu_threads=cpu_threads,
        num_batch=num_batch,
        profile_name=profile_name,
        model_size_gb=model_size,
        required_memory_gb=required,
        available_memory_gb=available,
        headroom_gb=headroom,
        quant_bits=candidate_quant_bits(candidate),
    )


def recommend_contribute_settings(
    hw: HardwareInfo,
    candidate: dict,
    logical_cpu_count: int | None = None,
) -> RuntimeProfile:
    """Return the versioned, hardware-independent benchmark shape.

    Offload and thread placement may still follow the machine, but context and
    batch are fixed so two contributions for the same model do not silently
    benchmark different memory/throughput workloads merely because one scan
    happened while a browser tab was busy.
    """
    base = recommend_runtime_settings(hw, candidate, logical_cpu_count)
    model_size = base.model_size_gb
    if hw.unified_memory:
        gpu_offload_percent = 100
    elif hw.vram_total_gb is None or model_size is None:
        gpu_offload_percent = 0
    else:
        # Benchmark placement must not change because another application
        # happened to use VRAM during this scan. The live gate handles that.
        reserve = max(VRAM_RESERVE_MIN_GB, hw.vram_total_gb * VRAM_RESERVE_RATIO)
        capacity = max(0.0, hw.vram_total_gb - reserve)
        if model_size <= capacity * 0.85:
            gpu_offload_percent = 100
        elif capacity <= 0:
            gpu_offload_percent = 0
        else:
            gpu_offload_percent = max(
                10, min(90, round((capacity * 0.8 / model_size) * 100))
            )
    return replace(
        base,
        context_length=CONTRIBUTE_CONTEXT_LENGTH,
        num_batch=CONTRIBUTE_NUM_BATCH,
        gpu_offload_percent=gpu_offload_percent,
        profile_name="contribute-v1",
    )


def contribute_ollama_options(
    profile: RuntimeProfile, metadata: Mapping[str, object]
) -> tuple[dict[str, int], int]:
    """Resolve a contribution offload percentage to Ollama layer count.

    Ollama's ``num_gpu`` is a layer count, not a percentage. Leaving it out
    for a partial profile silently asks Ollama to choose its own placement,
    making the memory estimate differ from the actual benchmark. If the GGUF
    layer count is unavailable, CPU-only is the only exact safe fallback.
    """
    options = profile.ollama_options
    percent = profile.gpu_offload_percent
    if percent <= 0:
        return options, 0
    if percent >= 100:
        return options, 100
    architecture = metadata.get("general.architecture")
    layers = (
        metadata.get(f"{architecture}.block_count")
        if isinstance(architecture, str) and architecture
        else None
    )
    if isinstance(layers, bool) or not isinstance(layers, (int, float)) or layers <= 0:
        options["num_gpu"] = 0
        return options, 0
    layer_count = max(1, min(int(layers), round(float(layers) * percent / 100)))
    options["num_gpu"] = layer_count
    return options, max(0, min(100, round(100 * layer_count / float(layers))))


def confidence_label(real_row_count: int) -> str:
    if real_row_count < 10:
        return "experimental"
    if real_row_count < 100:
        return "growing"
    return "community measured"
