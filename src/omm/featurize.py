"""Shared feature extraction for the recommendation model - used both when
building training rows (scripts/train_model.py) and when scoring candidate
models at `omm recommend` time, so both sides encode features identically.
"""

from __future__ import annotations

import math
import re

# Order matters: this is the exact input vector the trained tree expects.
# New features are only appended.  Never insert or reorder entries: cached
# artifacts contain integer feature indexes, so preserving the original
# prefix keeps every older model scorable.
FEATURE_ORDER = [
    "ram_gb",
    "vram_gb",
    "unified_memory",
    "param_count_b",
    "quant_bits",
    "cpu_score",
    "cpu_tier",
    "gpu_score",
    "gpu_tier",
    "model_size_gb",
    "gpu_tflops",
    "context_length",
    "gpu_offload_ratio",
    "cpu_threads",
    "num_batch",
    "active_param_count_b",
    "engine_llamacpp",
    "engine_lmstudio",
]

_QUANT_BITS = {
    "Q1": 1.0,
    "Q2": 2.0,
    "Q3": 3.0,
    "Q4": 4.0,
    "Q5": 5.0,
    "Q6": 6.0,
    "Q7": 7.0,
    "Q8": 8.0,
    "F16": 16.0,
    "FP16": 16.0,
    "F32": 32.0,
    "FP32": 32.0,
}

# Ordinal tier bump within a chip generation - e.g. "M2 Pro" and "M2 Max"
# share the same generation number but aren't the same chip, so the
# generation number alone would collapse them. Highest matching word wins.
_TIER_WORDS = {
    "ultra": 3.0,
    "max": 2.0,
    "pro": 1.0,
    "ti": 1.0,
    "x3d": 1.0,
    "super": 0.5,
}

_CHIP_MODEL_RE = re.compile(
    r"\bM(\d+)\b|\b(?:i[3579]-?|Ryzen\s*\d\s*|RTX\s?|GTX\s?)(\d{3,5})",
    re.IGNORECASE,
)

_MMPROJ_RE = re.compile(r"mmproj", re.IGNORECASE)
_GPT_OSS_SIZE_RE = re.compile(
    r"(?:^|[/_.:-])gpt[-_.]?oss(?:[-_.:]|$).*?(20|120)[Bb](?=[-_.:]|$)",
    re.IGNORECASE,
)
_GPT_OSS_ACTIVE_PARAMS_B = {
    "20": 3.6,
    "120": 5.1,
}


def is_mmproj_filename(filename: str) -> bool:
    """llama.cpp's conversion tooling always names multimodal-projector
    GGUFs with "mmproj" in the filename. These aren't standalone models -
    they have no tokenizer/vocabulary of their own - so callers that
    auto-pick or rank a repo's .gguf files should exclude them rather than
    let one outrank or stand in for the real model quants."""
    return bool(_MMPROJ_RE.search(filename))


def parse_param_count_billions(text: str) -> float | None:
    # Strip repository-owner tokens because authors such as ``hauser458b``
    # look like parameter counts. Within a model filename prefer a B-unit
    # match over a later M-unit context-window token (for example the ``1M``
    # in ``Qwen2.5-7B-Instruct-1M``).
    without_owners = re.sub(r"(?:(?<=^)|(?<=\s))[A-Za-z0-9._-]+(?=/)", "", text)
    matches = list(
        re.finditer(
            r"(?<![A-Za-z0-9])(\d+(?:[._]\d+)?)([BbMm])(?=[-_.]|$)",
            without_owners,
        )
    )
    if not matches:
        return None
    billion_matches = [match for match in matches if match.group(2).lower() == "b"]
    m = (billion_matches or matches)[-1]
    value = float(m.group(1).replace("_", "."))
    return value / 1000.0 if m.group(2).lower() == "m" else value


def candidate_parameter_count_billions(candidate: dict) -> float | None:
    """Prefer verified candidate metadata, then filename-derived values."""
    explicit = _positive_finite_number(candidate.get("parameter_count_b"))
    if explicit is not None:
        return explicit
    sources = [str(candidate.get("filename") or "")]
    repo_id = str(candidate.get("repo_id") or "")
    if repo_id:
        sources.append(repo_id.rsplit("/", 1)[-1])
    sources.append(str(candidate.get("name") or ""))
    for source in sources:
        parsed = parse_param_count_billions(source)
        if parsed is not None:
            return parsed
    return None


def parse_active_param_count_billions(text: str) -> float | None:
    """Parse MoE active parameters from names such as ``35B-A3B``."""
    matches = re.findall(
        r"(?:^|[-_.])A(\d+(?:[._]\d+)?)[Bb](?=[-_.]|$)",
        text,
        re.IGNORECASE,
    )
    if not matches:
        return None
    return float(matches[-1].replace("_", "."))


def _known_gpt_oss_active_parameter_count(candidate: dict) -> float | None:
    """Return the model author's published active count for gpt-oss.

    Older telemetry recorded total parameters in the active field for these
    models. Keeping this narrow compatibility correction makes those
    historical rows usable while new local Ollama measurements provide a
    tensor-derived value.
    """
    sources = [
        str(candidate.get("filename") or ""),
        str(candidate.get("repo_id") or ""),
        str(candidate.get("name") or ""),
        str(candidate.get("model_installed") or ""),
    ]
    for source in sources:
        match = _GPT_OSS_SIZE_RE.search(source)
        if match:
            return _GPT_OSS_ACTIVE_PARAMS_B[match.group(1)]
    return None


def candidate_active_parameter_count_billions(candidate: dict) -> float | None:
    explicit = _positive_finite_number(candidate.get("active_parameter_count_b"))
    known_gpt_oss = _known_gpt_oss_active_parameter_count(candidate)
    if known_gpt_oss is not None:
        total = candidate_parameter_count_billions(candidate)
        # Preserve a new tensor-derived value, but repair the historical bug
        # where the "active" value was merely the total/name-parsed count.
        if explicit is None or (total is not None and explicit >= total * 0.8):
            return known_gpt_oss
    if explicit is not None:
        return explicit
    sources = [str(candidate.get("filename") or "")]
    repo_id = str(candidate.get("repo_id") or "")
    if repo_id:
        sources.append(repo_id.rsplit("/", 1)[-1])
    sources.append(str(candidate.get("name") or ""))
    for source in sources:
        parsed = parse_active_param_count_billions(source)
        if parsed is not None:
            return parsed
    if candidate.get("is_moe") is True:
        return None
    return candidate_parameter_count_billions(candidate)


def parse_quant_bits(text: str) -> float | None:
    # Prefer an explicit GGUF Q/IQ token over source or auxiliary precision.
    # This handles both ``BF16.Q4_K_M`` and ``Q4_K_M-fp16``. Requiring a
    # delimiter after the digit avoids treating an auxiliary ``Q8nextn``
    # suffix as the main quantization.
    upper = text.upper()
    quantized = re.findall(r"I?(Q[1-8])(?=[-_.]|$|XS|NL)", upper)
    if quantized:
        return _QUANT_BITS.get(quantized[-1])
    compact_float = re.findall(r"(?:MX|NV)FP([1-8])(?=[-_.]|$)", upper)
    if compact_float:
        return float(compact_float[-1])
    integer_quant = re.findall(r"(?:^|[-_.])I([1-8])(?=[-_.]|$)", upper)
    if integer_quant:
        return float(integer_quant[-1])
    bit_labels = re.findall(r"([1-8])[-_]?BIT(?=[-_.]|$)", upper)
    if bit_labels:
        return float(bit_labels[-1])
    precision = re.findall(r"(BF16|FP16|F16|FP32|F32)(?=[-_.]|$)", upper)
    if not precision:
        return None
    return 16.0 if precision[-1].endswith("16") else 32.0


def candidate_quant_bits(candidate: dict) -> float | None:
    """Read an explicit quantization or infer effective stored bits from size."""
    explicit = _positive_finite_number(candidate.get("quant_bits"))
    if explicit is not None:
        return explicit
    sources = [str(candidate.get("filename") or "")]
    repo_id = str(candidate.get("repo_id") or "")
    if repo_id:
        sources.append(repo_id.rsplit("/", 1)[-1])
    sources.append(str(candidate.get("name") or ""))
    for source in sources:
        parsed = parse_quant_bits(source)
        if parsed is not None:
            return parsed

    parameters = candidate_parameter_count_billions(candidate)
    size_bytes = candidate.get("size_bytes")
    if parameters and (size_value := _positive_finite_number(size_bytes)) is not None:
        effective_bits = size_value * 8 / (parameters * 1_000_000_000)
        if 0.5 <= effective_bits <= 32:
            return round(effective_bits, 2)
    return None


def _positive_finite_number(value: object) -> float | None:
    """Return a usable numeric metadata value, excluding bool and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def parse_chip_score(text: str) -> tuple[float, float]:
    """Best-effort (model_number, tier_ordinal) parsed from a raw CPU/GPU
    name string, e.g. "Apple M2 Pro" -> (2.0, 1.0), "RTX 4090" -> (4090.0, 0.0).
    Returns (0.0, 0.0) for unrecognized or empty input."""
    m = _CHIP_MODEL_RE.search(text)
    score = float(m.group(1) or m.group(2)) if m else 0.0
    lowered = text.lower()
    tier = max((v for w, v in _TIER_WORDS.items() if w in lowered), default=0.0)
    return score, tier


def estimate_model_size_gb(text: str, size_bytes: int | float | None = None) -> float | None:
    """Return the GGUF file size in GiB, preferring Hub metadata.

    The parameter/quantization fallback is intentionally conservative: GGUF
    files contain metadata and tensors that are not captured by the simple
    ``parameters * bits`` calculation.
    """
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        return float(size_bytes) / (1024**3)

    param_count_b = parse_param_count_billions(text)
    quant_bits = parse_quant_bits(text)
    if param_count_b is None or quant_bits is None:
        return None
    return param_count_b * quant_bits / 8.0 * 1.1


def build_features(
    ram_gb: float,
    vram_gb: float | None,
    unified_memory: bool,
    param_count_b: float | None,
    quant_bits: float | None,
    cpu_score: float = 0.0,
    cpu_tier: float = 0.0,
    gpu_score: float = 0.0,
    gpu_tier: float = 0.0,
    model_size_gb: float = 0.0,
    gpu_tflops: float = 0.0,
    context_length: float = 0.0,
    gpu_offload_ratio: float = 0.0,
    cpu_threads: float = 0.0,
    num_batch: float = 0.0,
    active_param_count_b: float | None = None,
    engine: str = "ollama",
) -> list[float]:
    """Fixed-order numeric feature vector matching FEATURE_ORDER, with
    missing values defaulted to 0.0 (the tree learns around this)."""
    return [
        ram_gb,
        vram_gb if vram_gb is not None else 0.0,
        1.0 if unified_memory else 0.0,
        param_count_b if param_count_b is not None else 0.0,
        quant_bits if quant_bits is not None else 0.0,
        cpu_score,
        cpu_tier,
        gpu_score,
        gpu_tier,
        model_size_gb,
        gpu_tflops,
        context_length,
        gpu_offload_ratio,
        cpu_threads,
        num_batch,
        (
            active_param_count_b
            if active_param_count_b is not None
            else param_count_b
            if param_count_b is not None
            else 0.0
        ),
        1.0 if engine == "llama.cpp" else 0.0,
        1.0 if engine == "lmstudio" else 0.0,
    ]
