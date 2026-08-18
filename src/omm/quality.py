"""Small, reproducible quality-and-speed evidence runs through Ollama.

This deliberately stays narrower than a leaderboard suite. It provides a
versioned local smoke evaluation while preserving enough metadata to repeat a
run and compare it with Localfit's speed recommendations.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import multiprocessing
import platform
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Callable

from omm.hardware import HardwareInfo
from omm import benchmark, linker, tuning
from omm.engines.base import LoopbackJsonClient, RuntimeAdapterError
from omm.engines.lmstudio import LMStudioAdapter

OLLAMA_HOST = "http://localhost:11434"
MAX_PACK_BYTES = 1_000_000
MAX_ITEMS = 100
MAX_PROMPT_CHARS = 10_000
_FINAL_NUMBER_RE = re.compile(r"FINAL\s*[:=]\s*([-+]?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_ANY_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

# v7 failure taxonomy (see docs/telemetry-v7.md). Every QualityEvaluationError
# carries one of these so callers can classify without parsing free-text
# messages - which may reference exception internals, paths, etc. that must
# never reach telemetry.
FAILURE_REASON_OUT_OF_MEMORY = "out_of_memory"
FAILURE_REASON_MODEL_LOAD_FAILED = "model_load_failed"
FAILURE_REASON_UNSUPPORTED_RUNTIME = "unsupported_runtime"
FAILURE_REASON_GENERATION_TIMEOUT = "generation_timeout"
FAILURE_REASON_OLLAMA_UNAVAILABLE = "ollama_unavailable"
FAILURE_REASON_CONNECTION_ERROR = "connection_error"
FAILURE_REASON_NO_TIMING_METRICS = "no_timing_metrics"
FAILURE_REASON_UNKNOWN = "unknown"
# Only ever produced by the explicit confirmation flow (see
# collect_evidence(..., confirm_performance_timeout=True) /
# _confirm_generation_timeout below) - never by a single, unconfirmed
# request. A lone generation_timeout stays a transient_error.
FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT = "confirmed_generation_timeout"

# A model/hardware combination that will never work regardless of retries.
# Deliberately narrow: only reasons Ollama's own response makes explicit.
# "model_load_failed" is NOT here - a missing/undownloaded file or a plain,
# undiagnosed load failure is not evidence the model doesn't fit this
# hardware (it may simply not be present yet, or hit a transient I/O
# error), so it lives in the transient lane below instead.
MODEL_UNFIT_REASONS = frozenset({
    FAILURE_REASON_OUT_OF_MEMORY,
    FAILURE_REASON_UNSUPPORTED_RUNTIME,
})
# A one-off/environmental/inconclusive hiccup that says nothing about
# whether the model fits this hardware.
TRANSIENT_ERROR_REASONS = frozenset({
    FAILURE_REASON_MODEL_LOAD_FAILED,
    FAILURE_REASON_GENERATION_TIMEOUT,
    FAILURE_REASON_OLLAMA_UNAVAILABLE,
    FAILURE_REASON_CONNECTION_ERROR,
    FAILURE_REASON_NO_TIMING_METRICS,
    FAILURE_REASON_UNKNOWN,
})
# A model that loads but is too slow to finish a generation twice in a row
# under identical conditions - a reproducible performance ceiling, not a
# one-off hiccup (transient_error) and not an outright load/OOM failure
# (model_unfit). Reached only through explicit, opt-in confirmation.
PERFORMANCE_UNFIT_REASONS = frozenset({
    FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT,
})
FAILURE_REASONS = MODEL_UNFIT_REASONS | TRANSIENT_ERROR_REASONS | PERFORMANCE_UNFIT_REASONS

# The standard, non-configurable per-request timeout used for generation
# calls (see _request_json's default). Confirmation-mode performance_unfit
# events record this exact value as timeout_seconds - it must never be
# shortened to manufacture a timeout.
DEFAULT_GENERATION_TIMEOUT_SECONDS = 180
# If the daemon is found dead partway through a multi-model collect_evidence
# run, give up after this many consecutive failed restart attempts rather
# than burning a full DEFAULT_GENERATION_TIMEOUT_SECONDS per remaining
# request-per-remaining-tag (mirrors omm contribute's daemon recovery loop).
_MAX_CONSECUTIVE_DAEMON_FAILURES = 3
_DAEMON_RESTART_BACKOFF_SECONDS = 5.0
# A client-side requests.ReadTimeout only ends *our* wait for a response -
# it says nothing about whether Ollama's own generation goroutine actually
# stopped. Before a confirmation attempt may run, we explicitly unload the
# model (Ollama's own stop/keep_alive=0 API) and then poll /api/ps until it
# actually reports the model gone, bounded by these two constants - never
# an indefinite wait, and never trusting a fixed sleep as proof of anything.
CONFIRMATION_UNLOAD_POLL_INTERVAL_SECONDS = 1
CONFIRMATION_UNLOAD_MAX_WAIT_SECONDS = 30


def outcome_for_failure_reason(failure_reason: str) -> str:
    """Map a failure_reason onto its fixed outcome lane.

    When classification is uncertain, callers should pick
    FAILURE_REASON_UNKNOWN rather than guess - this function then reports
    "transient_error" rather than "model_unfit", per the project's policy of
    never over-claiming that a model is unfit for hardware it merely had a
    one-off problem on.
    """
    if failure_reason in MODEL_UNFIT_REASONS:
        return "model_unfit"
    if failure_reason in PERFORMANCE_UNFIT_REASONS:
        return "performance_unfit"
    return "transient_error"


class QualityEvaluationError(RuntimeError):
    """Raised when the pack or local Ollama response cannot be trusted.

    Carries a structured `failure_reason` (one of FAILURE_REASONS) so
    `collect_evidence` can build v7 failure telemetry without inspecting
    the free-text message, which may embed paths or exception internals
    that must never be sent to Firebase.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_reason: str = FAILURE_REASON_UNKNOWN,
        gpu_crash: bool = False,
    ) -> None:
        super().__init__(message)
        if failure_reason not in FAILURE_REASONS:
            failure_reason = FAILURE_REASON_UNKNOWN
        self.failure_reason = failure_reason
        # Local-only signal (never uploaded): the GPU backend itself
        # faulted on a stale/mismatched driver, so a same-candidate
        # CPU-only retry has a real chance of succeeding. See
        # RuntimeAdapterError._is_gpu_driver_crash for what sets this.
        self.gpu_crash = gpu_crash


class QualityEvaluationCancelled(RuntimeError):
    """The parent contribution session requested an immediate stop."""


@dataclass(frozen=True)
class SpeedSummary:
    median_tokens_per_sec: float
    samples: tuple[float, ...]


def default_pack_path() -> Path:
    return Path(str(files("omm").joinpath("data/quality-pack-v1.json")))


def _canonical_pack_bytes(pack: dict) -> bytes:
    return json.dumps(pack, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _bounded_int(value, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QualityEvaluationError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def load_pack(path: Path | None = None) -> tuple[dict, str]:
    pack_path = path or default_pack_path()
    try:
        raw = pack_path.read_bytes()
    except OSError as error:
        raise QualityEvaluationError(f"could not read quality pack {pack_path}: {error}") from error
    if len(raw) > MAX_PACK_BYTES:
        raise QualityEvaluationError("quality pack exceeds the 1 MB safety limit")
    try:
        pack = json.loads(raw)
    except json.JSONDecodeError as error:
        raise QualityEvaluationError(f"quality pack is not valid JSON: {error}") from error
    if not isinstance(pack, dict) or pack.get("schema_version") != 1:
        raise QualityEvaluationError("quality pack must be a schema-version 1 object")
    if not isinstance(pack.get("pack_id"), str) or not pack["pack_id"]:
        raise QualityEvaluationError("quality pack requires a non-empty pack_id")
    template = pack.get("prompt_template")
    if not isinstance(template, str) or template.count("{question}") != 1:
        raise QualityEvaluationError("prompt_template must contain {question} exactly once")
    if len(template) > MAX_PROMPT_CHARS:
        raise QualityEvaluationError("prompt_template is too long")
    generation = pack.get("generation")
    if not isinstance(generation, dict):
        raise QualityEvaluationError("quality pack requires generation settings")
    _bounded_int(generation.get("seed"), 0, 2**31 - 1, "generation.seed")
    _bounded_int(generation.get("num_ctx"), 256, 131_072, "generation.num_ctx")
    _bounded_int(generation.get("num_predict"), 1, 512, "generation.num_predict")
    temperature = generation.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise QualityEvaluationError("generation.temperature must be numeric")
    if not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
        raise QualityEvaluationError("generation.temperature must be from 0 to 2")
    if not isinstance(generation.get("think"), bool):
        raise QualityEvaluationError("generation.think must be boolean")

    items = pack.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise QualityEvaluationError(f"quality pack must contain 1 to {MAX_ITEMS} items")
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise QualityEvaluationError("every quality item must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
            raise QualityEvaluationError("quality item IDs must be unique non-empty strings")
        seen_ids.add(item_id)
        if item.get("answer_type") != "number":
            raise QualityEvaluationError(f"{item_id}: only number answers are supported")
        for field in ("category", "language", "question", "expected"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise QualityEvaluationError(f"{item_id}: {field} must be a non-empty string")
        if len(item["question"]) > MAX_PROMPT_CHARS:
            raise QualityEvaluationError(f"{item_id}: question is too long")
        if _normalize_number(item["expected"]) is None:
            raise QualityEvaluationError(f"{item_id}: expected is not a valid number")
    return pack, hashlib.sha256(_canonical_pack_bytes(pack)).hexdigest()


def _normalize_number(value: str) -> str | None:
    try:
        number = Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not number.is_finite():
        return None
    normalized = format(number.normalize(), "f")
    return "0" if normalized in ("-0", "+0") else normalized


def parse_numeric_answer(response: str) -> str | None:
    match = _FINAL_NUMBER_RE.search(response)
    if match:
        return _normalize_number(match.group(1))
    matches = _ANY_NUMBER_RE.findall(response)
    return _normalize_number(matches[-1]) if matches else None


_OOM_MARKERS = (
    "out of memory", "requires more system memory", "requires more than",
    "not enough memory", "cuda out of memory", "insufficient memory",
    "requires more available memory",
)
# Deliberately maps to the transient lane (FAILURE_REASON_MODEL_LOAD_FAILED
# is in TRANSIENT_ERROR_REASONS, not MODEL_UNFIT_REASONS): "failed to load"
# covers a missing/undownloaded file, a corrupted one, or any other
# undiagnosed load error just as often as a real hardware mismatch, so it
# is never treated as proof the model doesn't fit this machine.
_MODEL_LOAD_FAILED_MARKERS = (
    "failed to load", "unable to load", "no slots available", "not found",
    "invalid model", "could not load",
)
_UNSUPPORTED_RUNTIME_MARKERS = ("does not support", "not supported", "unsupported")


def _classify_error_response(response) -> str:
    """Best-effort classification from Ollama's own error body.

    Only used to pick a fixed enum value locally - the message text itself
    is discarded and never forwarded to telemetry.
    """
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("error", ""))
    except ValueError:
        pass
    if not message:
        try:
            message = response.text[:2000]
        except Exception:
            message = ""
    lowered = message.lower()
    if any(marker in lowered for marker in _OOM_MARKERS):
        return FAILURE_REASON_OUT_OF_MEMORY
    if any(marker in lowered for marker in _MODEL_LOAD_FAILED_MARKERS):
        return FAILURE_REASON_MODEL_LOAD_FAILED
    if any(marker in lowered for marker in _UNSUPPORTED_RUNTIME_MARKERS):
        return FAILURE_REASON_UNSUPPORTED_RUNTIME
    return FAILURE_REASON_UNKNOWN


def _request_json(
    method: str, path: str, payload: dict | None = None, timeout: int = DEFAULT_GENERATION_TIMEOUT_SECONDS
) -> dict:
    try:
        return LoopbackJsonClient(OLLAMA_HOST).request(
            method,
            path,
            payload=payload,
            timeout=timeout,
            default_failure="unknown",
            timeout_failure="generation_timeout",
        ).data
    except RuntimeAdapterError as error:
        if error.transport_kind == "connection_error":
            failure_reason = FAILURE_REASON_CONNECTION_ERROR
        elif error.transport_kind == "connect_timeout" or error.reason == "server_unavailable":
            failure_reason = FAILURE_REASON_OLLAMA_UNAVAILABLE
        elif error.reason == "generation_timeout":
            failure_reason = FAILURE_REASON_GENERATION_TIMEOUT
        elif error.reason == "out_of_memory":
            failure_reason = FAILURE_REASON_OUT_OF_MEMORY
        elif error.reason in {"load_failed", "model_not_visible"}:
            failure_reason = FAILURE_REASON_MODEL_LOAD_FAILED
        elif error.reason == "unsupported_runtime":
            failure_reason = FAILURE_REASON_UNSUPPORTED_RUNTIME
        else:
            failure_reason = FAILURE_REASON_UNKNOWN
        raise QualityEvaluationError(
            f"Ollama {path} request failed",
            failure_reason=failure_reason,
            gpu_crash=error.gpu_crash,
        ) from error


def ollama_version() -> str | None:
    try:
        value = _request_json("GET", "/api/version", timeout=5).get("version")
    except QualityEvaluationError:
        return None
    return value if isinstance(value, str) and len(value) <= 64 else None


def _tag_matches(name: object, tag: str) -> bool:
    """Ollama's /api/tags always suffixes a tag (default 'latest'), while omm
    passes around bare tags. Treat 'mmproj' and 'mmproj:latest' as the same
    model instead of failing the lookup on an implicit-suffix mismatch."""
    if not isinstance(name, str):
        return False
    if name == tag:
        return True
    return ":" not in tag and name == f"{tag}:latest"


def _tensor_parameter_count(tensor: object) -> int | None:
    """Return the number of logical parameters represented by an Ollama
    verbose ``/api/show`` tensor entry.

    Quantization changes storage size, not the tensor's logical shape, so
    multiplying the dimensions works for BF16 and quantized expert weights
    alike. Reject malformed shapes instead of guessing.
    """
    if not isinstance(tensor, dict):
        return None
    shape = tensor.get("shape")
    if not isinstance(shape, list) or not shape:
        return None
    dimensions: list[int] = []
    for value in shape:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        dimensions.append(value)
    return math.prod(dimensions)


def _moe_active_parameter_count_billions(
    model_info: object,
    tensors: object,
) -> float | None:
    """Derive active parameters for a GGUF MoE model from its tensor shapes.

    ``total * experts_used / experts`` is wrong because attention, routing,
    norms and the output projection are active for every token. Instead,
    scale only routed ``*_exps`` tensors and keep the remaining tensors
    active. Input embeddings are excluded when a separate output projection
    exists, matching the standard "active parameters per token" definition
    used by model authors such as OpenAI's gpt-oss model card.

    The calculation is accepted only when the verbose tensor inventory sums
    exactly to ``general.parameter_count``. Any unfamiliar or incomplete
    response returns ``None`` rather than emitting another plausible-looking
    but wrong training feature.
    """
    if not isinstance(model_info, dict) or not isinstance(tensors, list):
        return None
    architecture = model_info.get("general.architecture")
    if not isinstance(architecture, str) or not architecture:
        return None

    total = model_info.get("general.parameter_count")
    expert_count = model_info.get(f"{architecture}.expert_count")
    expert_used_count = model_info.get(f"{architecture}.expert_used_count")
    values = (total, expert_count, expert_used_count)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        return None
    if expert_count <= 1 or expert_used_count > expert_count:
        return None

    tensor_counts: list[tuple[str, int]] = []
    for tensor in tensors:
        count = _tensor_parameter_count(tensor)
        name = tensor.get("name") if isinstance(tensor, dict) else None
        if count is None or not isinstance(name, str):
            return None
        tensor_counts.append((name, count))

    tensor_total = sum(count for _name, count in tensor_counts)
    if tensor_total != total:
        return None

    expert_total = sum(count for name, count in tensor_counts if "_exps." in name)
    if expert_total <= 0 or expert_total >= total:
        return None

    # When input and output embeddings are separate, the input lookup does
    # not activate the full vocabulary matrix for each token. Keep a tied
    # embedding counted when no distinct output projection exists.
    names = {name for name, _count in tensor_counts}
    input_embedding = (
        next((count for name, count in tensor_counts if name == "token_embd.weight"), 0)
        if "output.weight" in names
        else 0
    )
    always_active = total - expert_total - input_embedding
    if always_active <= 0:
        return None

    active = always_active + expert_total * expert_used_count / expert_count
    if not math.isfinite(active) or not 0 < active <= total:
        return None
    return active / 1_000_000_000


def list_benchmarkable_tags() -> list[str]:
    """All Ollama tags that could plausibly be benchmarked right now.

    Excludes mmproj/clip projector models (see _model_metadata) - they
    have no tokenizer of their own and would just fail every time.
    """
    tags = _request_json("GET", "/api/tags", timeout=10).get("models")
    if not isinstance(tags, list):
        return []
    names = []
    for item in tags:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        details = item.get("details")
        if not isinstance(name, str):
            continue
        if isinstance(details, dict) and details.get("family") == "clip":
            continue
        names.append(name)
    return sorted(names)


def _lmstudio_model_metadata(tag: str, lmstudio_model: dict | None) -> dict:
    """Map a `linker.resolve_lmstudio_model()` result onto the same
    metadata dict shape `_model_metadata` returns for Ollama (see the
    design's field-mapping table): family<-architecture,
    quantization_level<-quantization_name, parameter_size<-params_string,
    capabilities<-["tools"] iff trained_for_tool_use, is_moe is always
    False (LM Studio has no equivalent of Ollama's verbose tensor
    inventory), and digest/size_bytes/format/license/license_link are
    always None (LM Studio models have no equivalent concepts).

    mmproj/embedding models are never seen here: resolve_lmstudio_model
    already filters to type == "llm" before returning a match.
    """
    if lmstudio_model is None:
        # Could mean "never downloaded/linked" as easily as "doesn't fit
        # this hardware" - same ambiguity Ollama's own not-installed case
        # has, so this stays in the transient lane, never model_unfit.
        raise QualityEvaluationError(
            f"LM Studio model '{tag}' is not installed or could not be resolved",
            failure_reason=FAILURE_REASON_MODEL_LOAD_FAILED,
        )
    return {
        "tag": tag,
        "digest": None,
        "size_bytes": None,
        "format": None,
        "family": lmstudio_model.get("architecture"),
        "parameter_size": lmstudio_model.get("params_string"),
        "quantization_level": lmstudio_model.get("quantization_name"),
        "license": None,
        "license_link": None,
        "capabilities": ["tools"] if lmstudio_model.get("trained_for_tool_use") else [],
        "is_moe": False,
    }


def _model_metadata(tag: str, *, engine: str = "ollama", lmstudio_model: dict | None = None) -> dict:
    if engine == "lmstudio":
        return _lmstudio_model_metadata(tag, lmstudio_model)
    tags = _request_json("GET", "/api/tags", timeout=10).get("models")
    if not isinstance(tags, list):
        raise QualityEvaluationError(
            "Ollama model list is missing", failure_reason=FAILURE_REASON_UNKNOWN
        )
    listed = next((item for item in tags if isinstance(item, dict) and _tag_matches(item.get("name"), tag)), None)
    if listed is None:
        # Not present locally - could mean "never downloaded" as easily as
        # "doesn't fit this hardware." FAILURE_REASON_MODEL_LOAD_FAILED is a
        # transient reason for exactly this ambiguity; never claim model_unfit
        # from an absent file alone.
        raise QualityEvaluationError(
            f"Ollama model '{tag}' is not installed", failure_reason=FAILURE_REASON_MODEL_LOAD_FAILED
        )
    listed_details = listed.get("details")
    if isinstance(listed_details, dict) and listed_details.get("family") == "clip":
        # A linked-but-broken mmproj (multimodal projector) model: it was
        # registered before omm refused to link these, or via a manual
        # `ollama create`. It has no tokenizer of its own, so /api/generate
        # would crash Ollama's llama-server rather than return quality/speed
        # results - fail fast with a clear reason instead of an opaque 500.
        raise QualityEvaluationError(
            f"Ollama model '{tag}' is a multimodal projector (mmproj), not a "
            "standalone text-generation model - it can't be benchmarked.",
            failure_reason=FAILURE_REASON_UNSUPPORTED_RUNTIME,
        )
    shown = _request_json("POST", "/api/show", {"model": tag}, timeout=30)
    details = shown.get("details") if isinstance(shown.get("details"), dict) else {}
    model_info = shown.get("model_info") if isinstance(shown.get("model_info"), dict) else {}
    architecture = model_info.get("general.architecture")
    expert_count = (
        model_info.get(f"{architecture}.expert_count")
        if isinstance(architecture, str) and architecture
        else None
    )
    expert_used_count = (
        model_info.get(f"{architecture}.expert_used_count")
        if isinstance(architecture, str) and architecture
        else None
    )
    is_moe = (
        isinstance(expert_count, int)
        and not isinstance(expert_count, bool)
        and expert_count > 1
        and isinstance(expert_used_count, int)
        and not isinstance(expert_used_count, bool)
        and 0 < expert_used_count <= expert_count
    )
    active_parameter_count_b = None
    if is_moe:
        # Keep dense-model metadata requests small. Only MoE models need the
        # verbose tensor inventory required for an exact routed-weight count.
        try:
            verbose = _request_json(
                "POST",
                "/api/show",
                {"model": tag, "verbose": True},
                timeout=30,
            )
        except QualityEvaluationError:
            verbose = {}
        active_parameter_count_b = _moe_active_parameter_count_billions(
            verbose.get("model_info"),
            verbose.get("tensors"),
        )
    capabilities = shown.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
    size_bytes = listed.get("size")
    metadata = {
        "tag": tag,
        "digest": listed.get("digest") if isinstance(listed.get("digest"), str) else None,
        "size_bytes": size_bytes if isinstance(size_bytes, int) and size_bytes > 0 else None,
        "format": details.get("format"),
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization_level": details.get("quantization_level"),
        "license": model_info.get("general.license"),
        "license_link": model_info.get("general.license.link"),
        "capabilities": [
            value
            for value in capabilities
            if isinstance(value, str) and len(value) <= 64
        ][:32],
        "is_moe": is_moe,
    }
    if active_parameter_count_b is not None:
        metadata["active_parameter_count_b"] = active_parameter_count_b
    return metadata


def _normalized_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def runtime_snapshot(tag: str, digest: str | None, options: dict) -> dict | None:
    """Return measured Ollama residency values, never inferred GPU use."""
    try:
        models = _request_json("GET", "/api/ps", timeout=10).get("models")
    except QualityEvaluationError:
        return None
    if not isinstance(models, list):
        return None
    expected_digest = _normalized_digest(digest)
    if expected_digest is not None:
        row = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and _normalized_digest(item.get("digest")) == expected_digest
            ),
            None,
        )
    else:
        row = next(
            (
                item
                for item in models
                if isinstance(item, dict) and _tag_matches(item.get("name"), tag)
            ),
            None,
        )
    if row is None:
        return None
    context, size, size_vram = row.get("context_length"), row.get("size"), row.get("size_vram")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (context, size, size_vram)):
        return None
    if context <= 0 or size <= 0 or size_vram < 0:
        return None
    threads, batch = options.get("num_thread"), options.get("num_batch")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (threads, batch)):
        return None
    return {
        "context_length": context,
        "gpu_offload_percent": max(0, min(100, round(100 * size_vram / size))),
        "cpu_threads": threads,
        "num_batch": batch,
        "runtime_profile": "explicit_ollama_options",
    }


def unload_model(tag: str, *, engine: str = "ollama") -> bool:
    """Best-effort isolation between models; never deletes model files."""
    if engine == "lmstudio":
        return linker.unload_lmstudio_model(tag)
    try:
        _request_json(
            "POST",
            "/api/generate",
            {"model": tag, "stream": False, "keep_alive": 0},
            timeout=30,
        )
    except QualityEvaluationError:
        return False
    return True


def _model_is_loaded(tag: str) -> bool | None:
    """Query Ollama's own residency list. True/False on a clear answer,
    None if the daemon itself couldn't be reached - callers must treat
    None as "not confirmed unloaded", never as "confirmed unloaded"."""
    try:
        models = _request_json("GET", "/api/ps", timeout=10).get("models")
    except QualityEvaluationError:
        return None
    if not isinstance(models, list):
        return None
    return any(isinstance(item, dict) and _tag_matches(item.get("name"), tag) for item in models)


def ensure_model_unloaded(
    tag: str,
    *,
    max_wait_seconds: float = CONFIRMATION_UNLOAD_MAX_WAIT_SECONDS,
    poll_interval_seconds: float = CONFIRMATION_UNLOAD_POLL_INTERVAL_SECONDS,
) -> bool:
    """Explicitly stop `tag` via Ollama's own API and prove it left memory.

    A requests.ReadTimeout on our side only ends our own wait for a
    response - it is not evidence that Ollama's internal generation
    goroutine actually stopped. This calls the same stop/keep_alive=0
    endpoint `unload_model` uses (never a subprocess kill, never restarting
    the daemon, never sudo), then polls GET /api/ps - Ollama's own
    residency list - until `tag` is actually absent from it. Polling is
    bounded by `max_wait_seconds`; it never runs indefinitely, and if the
    model still can't be confirmed gone by the deadline (or /api/ps itself
    can't be queried), this returns False so the caller can refuse to start
    a second, possibly-overlapping generation request.
    """
    unload_model(tag)
    elapsed = 0.0
    while True:
        loaded = _model_is_loaded(tag)
        if loaded is False:
            return True
        if loaded is None:
            # The daemon/API itself is unavailable. Waiting cannot confirm a
            # handle release and only stalls cleanup; the caller's bounded
            # file-unlink retries will still surface a real Windows lock.
            return False
        if elapsed >= max_wait_seconds:
            return False
        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds


def _generate(tag: str, prompt: str, generation: dict, num_predict: int | None = None,
              runtime_options: dict | None = None, supports_thinking: bool = True) -> dict:
    options = {
        "temperature": generation["temperature"],
        "seed": generation["seed"],
        "num_ctx": generation["num_ctx"],
        "num_predict": num_predict or generation["num_predict"],
    }
    options.update(runtime_options or {})
    payload = {
        "model": tag,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }
    if supports_thinking:
        # Ollama rejects the top-level `think` field outright - HTTP 400
        # "does not support thinking" - for any model whose capabilities
        # don't list "thinking", even when the value is False. Omitting the
        # field entirely is the documented-safe choice for those models.
        payload["think"] = generation["think"]
    data = _request_json("POST", "/api/generate", payload)
    if not isinstance(data.get("response"), str):
        raise QualityEvaluationError(
            f"Ollama returned no text response for '{tag}'", failure_reason=FAILURE_REASON_UNKNOWN
        )
    return data


def _generate_with_runtime(
    tag: str, prompt: str, generation: dict, num_predict: int | None, runtime_options: dict | None,
    supports_thinking: bool = True,
    *, engine: str = "ollama", lmstudio_port: int | None = None,
) -> dict:
    """Avoid changing the call shape used by older test/integration fakes."""
    if engine == "lmstudio":
        return _generate_lmstudio(tag, prompt, generation, num_predict, lmstudio_port)
    kwargs = {}
    if runtime_options is not None:
        kwargs["runtime_options"] = runtime_options
    if not supports_thinking:
        kwargs["supports_thinking"] = False
    return _generate(tag, prompt, generation, num_predict=num_predict, **kwargs)


def _lmstudio_request_json(
    port: int, method: str, path: str, payload: dict | None = None,
    timeout: int | float = DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> dict:
    """Same connection/timeout/HTTP-error failure_reason classification as
    _request_json above, just pointed at LM Studio's loopback port instead
    of the fixed Ollama host - intentionally duplicated rather than shared
    so this new LM Studio transport can never change _request_json's
    already-tested Ollama behavior."""
    try:
        return LoopbackJsonClient(f"http://127.0.0.1:{port}").request(
            method,
            path,
            payload=payload,
            timeout=timeout,
            default_failure="unknown",
            timeout_failure="generation_timeout",
        ).data
    except RuntimeAdapterError as error:
        if error.transport_kind == "connection_error":
            failure_reason = FAILURE_REASON_CONNECTION_ERROR
        elif error.transport_kind == "connect_timeout" or error.reason == "server_unavailable":
            failure_reason = FAILURE_REASON_OLLAMA_UNAVAILABLE
        elif error.reason == "generation_timeout":
            failure_reason = FAILURE_REASON_GENERATION_TIMEOUT
        elif error.reason == "out_of_memory":
            failure_reason = FAILURE_REASON_OUT_OF_MEMORY
        elif error.reason in {"load_failed", "model_not_visible"}:
            failure_reason = FAILURE_REASON_MODEL_LOAD_FAILED
        elif error.reason == "unsupported_runtime":
            failure_reason = FAILURE_REASON_UNSUPPORTED_RUNTIME
        else:
            failure_reason = FAILURE_REASON_UNKNOWN
        raise QualityEvaluationError(
            f"LM Studio {path} request failed",
            failure_reason=failure_reason,
            gpu_crash=error.gpu_crash,
        ) from error


def _generate_lmstudio(
    model_key: str, prompt: str, generation: dict, num_predict: int | None, port: int | None,
) -> dict:
    """POST /api/v0/chat/completions and normalize the response into
    Ollama's response shape at this transport boundary:
    {"response": <text>, "eval_count": <completion_tokens>,
     "eval_duration": <int(generation_time * 1e9)>}.

    This is the ONLY place LM Studio's response shape is known about -
    accuracy scoring (which reads response["response"]) and
    _tokens_per_second (which reads eval_count/eval_duration) stay
    completely engine-agnostic below this point.
    """
    if port is None:
        # collect_evidence's own daemon-health loop is responsible for
        # catching a dead/unreachable LM Studio server before ever calling
        # in here; a None port getting this far means the caller (e.g. a
        # direct evaluate_model(..., engine="lmstudio") call) never
        # resolved one. Fail soft with the same taxonomy an unreachable
        # daemon uses rather than raising a bare TypeError.
        raise QualityEvaluationError(
            "LM Studio server port is unavailable", failure_reason=FAILURE_REASON_OLLAMA_UNAVAILABLE,
        )
    payload = {
        "model": model_key,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": num_predict or generation["num_predict"],
        "temperature": generation["temperature"],
        "stream": False,
    }
    data = _lmstudio_request_json(port, "POST", "/api/v0/chat/completions", payload)
    choices = data.get("choices")
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise QualityEvaluationError(
            f"LM Studio returned no text response for '{model_key}'", failure_reason=FAILURE_REASON_UNKNOWN,
        )
    result: dict = {"response": text}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    completion_tokens = usage.get("completion_tokens")
    if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool) and completion_tokens > 0:
        result["eval_count"] = completion_tokens
    generation_time = stats.get("generation_time")
    if (
        isinstance(generation_time, (int, float))
        and not isinstance(generation_time, bool)
        and generation_time > 0
    ):
        result["eval_duration"] = int(generation_time * 1_000_000_000)
    return result


def _tokens_per_second(response: dict) -> float | None:
    count = response.get("eval_count")
    duration = response.get("eval_duration")
    if (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
        and isinstance(duration, int)
        and not isinstance(duration, bool)
        and duration > 0
    ):
        return count / (duration / 1_000_000_000)
    return None


def _speed_probe(
    tag: str, generation: dict, runs: int, runtime_options: dict | None = None, supports_thinking: bool = True,
    *, engine: str = "ollama", lmstudio_port: int | None = None,
) -> SpeedSummary:
    _bounded_int(runs, 1, 10, "speed runs")
    prompt = "Explain what an operating system is in a concise paragraph."
    _generate_with_runtime(
        tag, prompt, generation, 8, runtime_options, supports_thinking, engine=engine, lmstudio_port=lmstudio_port
    )
    samples = []
    for _ in range(runs):
        speed = _tokens_per_second(
            _generate_with_runtime(
                tag, prompt, generation, 64, runtime_options, supports_thinking,
                engine=engine, lmstudio_port=lmstudio_port,
            )
        )
        if speed is None:
            engine_label = "LM Studio" if engine == "lmstudio" else "Ollama"
            raise QualityEvaluationError(
                f"{engine_label} returned no timing metrics for '{tag}'",
                failure_reason=FAILURE_REASON_NO_TIMING_METRICS,
            )
        samples.append(speed)
    return SpeedSummary(statistics.median(samples), tuple(samples))


def evaluate_model(
    tag: str, pack: dict, speed_runs: int = 3, runtime_options: dict | None = None,
    model_metadata: dict | None = None,
    *,
    engine: str = "ollama",
    lmstudio_port: int | None = None,
    lmstudio_model: dict | None = None,
) -> dict:
    if model_metadata is not None:
        metadata = model_metadata
    elif engine == "lmstudio":
        metadata = _model_metadata(tag, engine="lmstudio", lmstudio_model=lmstudio_model)
    else:
        metadata = _model_metadata(tag)
    supports_thinking = "thinking" in (metadata.get("capabilities") or [])
    template = pack["prompt_template"]
    generation = pack["generation"]
    item_results = []
    quality_speeds = []
    for item in pack["items"]:
        response = _generate_with_runtime(
            tag, template.format(question=item["question"]), generation, None, runtime_options, supports_thinking,
            engine=engine, lmstudio_port=lmstudio_port,
        )
        predicted = parse_numeric_answer(response["response"])
        expected = _normalize_number(item["expected"])
        correct = predicted is not None and predicted == expected
        speed = _tokens_per_second(response)
        if speed is not None:
            quality_speeds.append(speed)
        item_results.append(
            {
                "id": item["id"],
                "category": item["category"],
                "language": item["language"],
                "expected": expected,
                "predicted": predicted,
                "correct": correct,
            }
        )

    correct_count = sum(1 for item in item_results if item["correct"])
    category_total = Counter(item["category"] for item in item_results)
    category_correct = Counter(item["category"] for item in item_results if item["correct"])
    speed = _speed_probe(
        tag, generation, speed_runs, runtime_options=runtime_options, supports_thinking=supports_thinking,
        engine=engine, lmstudio_port=lmstudio_port,
    )
    return {
        **metadata,
        "engine": engine,
        "quality": {
            "correct": correct_count,
            "total": len(item_results),
            "accuracy": round(correct_count / len(item_results), 4),
            "by_category": {
                category: {
                    "correct": category_correct[category],
                    "total": total,
                    "accuracy": round(category_correct[category] / total, 4),
                }
                for category, total in sorted(category_total.items())
            },
            "items": item_results,
            "raw_responses_stored": False,
        },
        "speed": {
            "median_tokens_per_sec": round(speed.median_tokens_per_sec, 2),
            "samples_tokens_per_sec": [round(value, 2) for value in speed.samples],
            "runs": len(speed.samples),
            "probe_num_predict": 64,
            "quality_generation_median_tokens_per_sec": (
                round(statistics.median(quality_speeds), 2) if quality_speeds else None
            ),
        },
        # LM Studio's GPU-offload/residency measurement isn't ported (see
        # design doc non-goals) - runtime_snapshot is Ollama's /api/ps only,
        # so the LM Studio path always reports this as None rather than
        # querying the wrong daemon.
        "runtime": (
            runtime_snapshot(tag, metadata.get("digest"), runtime_options or {})
            if engine != "lmstudio"
            else None
        ),
    }


def _evaluate_model_worker(
    send_conn,
    tag: str,
    pack: dict,
    speed_runs: int,
    runtime_options: dict | None,
    model_metadata: dict | None,
) -> None:
    """Spawn-safe worker for one killable Ollama evaluation."""
    try:
        result = evaluate_model(
            tag,
            pack,
            speed_runs=speed_runs,
            runtime_options=runtime_options,
            model_metadata=model_metadata,
        )
        send_conn.send(("ok", result))
    except QualityEvaluationError as error:
        send_conn.send(("quality_error", str(error), error.failure_reason, error.gpu_crash))
    except BaseException as error:
        send_conn.send(("unexpected_error", type(error).__name__))
    finally:
        send_conn.close()


_DEFAULT_EVALUATE_MODEL = evaluate_model


def evaluate_model_isolated(
    tag: str,
    pack: dict,
    *,
    speed_runs: int = 3,
    runtime_options: dict | None = None,
    model_metadata: dict | None = None,
    timeout_seconds: float = 600,
    stop_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[float, float], None] | None = None,
) -> dict:
    """Evaluate in a child process with an absolute wall-clock deadline.

    Requests' read timeout is not a total operation deadline, and a wedged
    native Ollama call must not hold an unattended contribution session for
    hours. A separate process lets the parent terminate the entire evaluator
    deterministically on Esc, Ctrl+C teardown, or deadline expiry.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    context = multiprocessing.get_context("spawn")
    receive_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_evaluate_model_worker,
        args=(send_conn, tag, pack, speed_runs, runtime_options, model_metadata),
        daemon=True,
    )
    started = time.monotonic()
    next_progress = started + 30
    process_started = False

    def terminate() -> None:
        if process_started and process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=5)

    try:
        process.start()
        process_started = True
        send_conn.close()
        deadline = started + timeout_seconds
        while True:
            if receive_conn.poll(0.2):
                try:
                    message = receive_conn.recv()
                except EOFError as error:
                    raise QualityEvaluationError(
                        "Isolated benchmark worker closed without a result",
                        failure_reason=FAILURE_REASON_UNKNOWN,
                    ) from error
                process.join(timeout=5)
                kind = message[0]
                if kind == "ok":
                    return message[1]
                if kind == "quality_error":
                    gpu_crash = message[3] if len(message) > 3 else False
                    raise QualityEvaluationError(
                        message[1], failure_reason=message[2], gpu_crash=gpu_crash
                    )
                raise QualityEvaluationError(
                    f"Isolated benchmark worker failed with {message[1]}",
                    failure_reason=FAILURE_REASON_UNKNOWN,
                )
            now = time.monotonic()
            if stop_check is not None and stop_check():
                terminate()
                raise QualityEvaluationCancelled(tag)
            if now >= deadline:
                terminate()
                raise QualityEvaluationError(
                    f"Benchmarking {tag} exceeded the {timeout_seconds:g}s session deadline",
                    failure_reason=FAILURE_REASON_GENERATION_TIMEOUT,
                )
            if not process.is_alive():
                process.join(timeout=1)
                raise QualityEvaluationError(
                    f"Isolated benchmark worker exited with code {process.exitcode}",
                    failure_reason=FAILURE_REASON_UNKNOWN,
                )
            if progress_callback is not None and now >= next_progress:
                progress_callback(now - started, timeout_seconds)
                next_progress = now + 30
    finally:
        terminate()
        receive_conn.close()
        send_conn.close()


def _build_failure_entry(
    tag: str,
    error: QualityEvaluationError,
    metadata: dict | None,
    profile: "tuning.RuntimeProfile | None",
    unloaded: bool,
) -> dict:
    """A v7-shaped failure result: no speed/sample fields are ever faked."""
    reason = error.failure_reason
    entry: dict = {
        "tag": tag,
        "outcome": outcome_for_failure_reason(reason),
        "failure_reason": reason,
        "measurement_isolation": {
            "unloaded_after_run": unloaded,
            "model_files_deleted": False,
        },
    }
    if metadata:
        # Best-effort: available whenever the failure happened after
        # /api/show succeeded (e.g. OOM during generation), absent when the
        # model couldn't even be looked up (e.g. not installed).
        entry["model_metadata"] = {
            "digest": metadata.get("digest"),
            "size_bytes": metadata.get("size_bytes"),
            "parameter_size": metadata.get("parameter_size"),
            "quantization_level": metadata.get("quantization_level"),
        }
    if profile is not None:
        # The runtime omm *attempted* to use - not a live /api/ps snapshot,
        # since a model that never loaded can't be introspected there. This
        # is exactly the signal a fit-classifier needs: "this hardware+
        # runtime combination failed for this model."
        entry["attempted_runtime"] = {
            "context_length": profile.context_length,
            "gpu_offload_percent": profile.gpu_offload_percent,
            "cpu_threads": profile.cpu_threads,
            "num_batch": profile.num_batch,
        }
    return entry


def _evaluate_tag_once(
    tag: str,
    hardware: HardwareInfo,
    pack: dict,
    speed_runs: int,
    *,
    engine: str = "ollama",
    lmstudio_port: int | None = None,
    lmstudio_model: dict | None = None,
) -> dict:
    """One full attempt for one tag: evaluate, then always unload.

    Returns either a success dict (outcome="success") or a v7 failure dict
    from _build_failure_entry (outcome in {"model_unfit", "transient_error"}).
    This is the single source of truth for "what does one benchmark attempt
    look like" - both the normal collect_evidence loop and the confirmation
    flow below call it, so a confirmation attempt is guaranteed to use the
    exact same model/runtime-selection logic as the first attempt.

    For engine="lmstudio", `tag` is a modelKey (see collect_evidence) and
    `profile.ollama_options` below is still computed (tuning.
    recommend_runtime_settings is engine-agnostic) but goes unused by the
    LM Studio transport, which ignores runtime_options entirely - it exists
    purely to keep this function's shape identical between engines.
    """
    metadata = None
    profile = None
    failure: QualityEvaluationError | None = None
    result: dict | None = None
    try:
        if engine == "lmstudio":
            metadata = _model_metadata(tag, engine="lmstudio", lmstudio_model=lmstudio_model)
        else:
            metadata = _model_metadata(tag)
        profile = tuning.recommend_runtime_settings(hardware, metadata)
        options = profile.ollama_options
        try:
            parameters = inspect.signature(evaluate_model).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {"speed_runs": speed_runs}
        if accepts_kwargs or "runtime_options" in parameters:
            kwargs["runtime_options"] = options
        if accepts_kwargs or "model_metadata" in parameters:
            kwargs["model_metadata"] = metadata
        if engine != "ollama":
            if accepts_kwargs or "engine" in parameters:
                kwargs["engine"] = engine
            if accepts_kwargs or "lmstudio_port" in parameters:
                kwargs["lmstudio_port"] = lmstudio_port
        result = evaluate_model(tag, pack, **kwargs)
    except QualityEvaluationError as error:
        # A real evaluator failure is one attempt and one outcome. Retrying
        # without runtime options would silently rerun timeouts/OOMs and could
        # exceed the documented two-attempt confirmation ceiling.
        failure = error
    finally:
        unloaded = unload_model(tag) if engine == "ollama" else unload_model(tag, engine=engine)
    if failure is not None or result is None:
        return _build_failure_entry(tag, failure, metadata, profile, unloaded)
    result["outcome"] = "success"
    result["measurement_isolation"] = {
        "unloaded_after_run": unloaded,
        "model_files_deleted": False,
    }
    result.setdefault("runtime", None)
    return result


def _confirm_generation_timeout(
    tag: str,
    hardware: HardwareInfo,
    pack: dict,
    speed_runs: int,
) -> dict:
    """At most one confirmation attempt after a first generation_timeout.

    Only ever called (by collect_evidence, opt-in) right after the first
    attempt for `tag` finished with outcome=transient_error/
    generation_timeout.

    A client-side requests.ReadTimeout only ends *our* wait for a response;
    it is not proof that Ollama's own generation goroutine actually
    stopped. Before the confirmation attempt is allowed to run, this
    explicitly stops the model via Ollama's own API and then proves it via
    bounded GET /api/ps polling (see `ensure_model_unloaded`) that the
    model has actually left memory - that is what guarantees the two
    generation requests never overlap inside the daemon, not a fixed
    sleep. If that can't be confirmed within the bounded wait, the second
    request is never issued at all.

    Returns exactly one final dict - never two, and never re-runs more than
    once - so the caller uploads exactly one telemetry event for this tag:
      - outcome=transient_error if the daemon/model isn't confirmed healthy
        beforehand, or if the model can't be confirmed unloaded in time
        (the second request is skipped entirely in that case).
      - outcome=performance_unfit only if the confirmation attempt *also*
        times out on generation while the daemon is otherwise healthy.
      - outcome=success if the confirmation attempt succeeds (real speed
        recorded, no fabricated 0). The confirmation attempt is allowed to
        be a cold start - both attempts exist to check the same standard,
        user-facing conditions, not to control for cache warmth.
      - outcome=model_unfit if the confirmation attempt hits an explicit
        OOM/unsupported-runtime error.
      - outcome=transient_error for anything else the confirmation attempt
        itself hits (daemon/connection trouble during that attempt).
    """
    # 6. Daemon health check before touching anything else.
    if ollama_version() is None:
        return _build_failure_entry(
            tag,
            QualityEvaluationError(
                "Ollama daemon was not reachable before the confirmation attempt",
                failure_reason=FAILURE_REASON_OLLAMA_UNAVAILABLE,
            ),
            None, None, True,
        )
    # 7. Same model still available.
    try:
        _model_metadata(tag)
    except QualityEvaluationError as error:
        return _build_failure_entry(tag, error, None, None, True)
    # Explicitly unload and prove it via bounded /api/ps polling - never
    # trust a fixed sleep as evidence the first generation actually ended
    # inside Ollama. Unload *failure* itself is never a model_unfit/
    # performance_unfit verdict - it just means we can't safely run a
    # second request, so we stop here with an honest transient_error and
    # never issue it.
    if not ensure_model_unloaded(tag):
        return _build_failure_entry(
            tag,
            QualityEvaluationError(
                "could not confirm the first generation was fully unloaded "
                "before a confirmation attempt",
                failure_reason=FAILURE_REASON_UNKNOWN,
            ),
            None, None, False,
        )
    # 9. Confirmation attempt, same model/runtime, exactly once. A cold
    # start here is fine by design (see docstring) - _evaluate_tag_once
    # unloads again in its own `finally` regardless of outcome.
    second = _evaluate_tag_once(tag, hardware, pack, speed_runs)
    outcome = second.get("outcome")
    if outcome == "transient_error" and second.get("failure_reason") == FAILURE_REASON_GENERATION_TIMEOUT:
        # 10. Confirmed twice under a healthy daemon: a reproducible
        # performance ceiling, not a one-off hiccup.
        second["outcome"] = "performance_unfit"
        second["failure_reason"] = FAILURE_REASON_CONFIRMED_GENERATION_TIMEOUT
        second["confirmation_attempts"] = 2
        second["timeout_seconds"] = DEFAULT_GENERATION_TIMEOUT_SECONDS
    # 11. success, 12. model_unfit, or 13. some other transient_error -
    # otherwise pass the confirmation attempt's own honest result through.
    # Best-effort final cleanup so the confirmation flow never leaves
    # memory pressure behind; a cleanup failure here must never change the
    # verdict already decided above.
    unload_model(tag)
    return second


def collect_evidence(
    tags: list[str],
    hardware: HardwareInfo,
    pack_path: Path | None = None,
    speed_runs: int = 3,
    *,
    engine: str = "ollama",
    lmstudio_models: dict[str, dict] | None = None,
    confirm_performance_timeout: bool = False,
    on_model_start: Callable[[str, int, int], None] | None = None,
    on_daemon_event: Callable[[str], None] | None = None,
) -> dict:
    """Evaluate `tags` against `engine`.

    For engine="ollama" (default), `tags` are Ollama tags exactly as
    before. For engine="lmstudio", `tags` are LM Studio modelKey strings
    (the caller resolves each via linker.resolve_lmstudio_model() ahead of
    time and passes the results in `lmstudio_models`, keyed by that same
    modelKey) - this keeps the `tags: list[str]` shape itself unchanged so
    callers matching results back via model["tag"] need no changes.

    confirm_performance_timeout's second-attempt confirmation flow
    (_confirm_generation_timeout) stays Ollama-only: it relies on
    ensure_model_unloaded's /api/ps-based "proven gone" polling, which has
    no LM Studio equivalent yet. A generation_timeout on the LM Studio path
    is reported as a plain transient_error, same as confirm mode disabled.
    """
    if not tags:
        raise QualityEvaluationError("at least one model tag is required")
    if len(tags) > 20:
        raise QualityEvaluationError("at most 20 models may be evaluated at once")
    if len(set(tags)) != len(tags) or any(not tag or len(tag) > 256 for tag in tags):
        raise QualityEvaluationError("model tags must be unique non-empty strings")
    if engine == "lmstudio" and not lmstudio_models:
        raise QualityEvaluationError("lmstudio_models is required when engine='lmstudio'")
    pack, pack_sha256 = load_pack(pack_path)
    models = []
    total = len(tags)
    consecutive_daemon_failures = 0
    lmstudio_port = linker.lmstudio_server_port() if engine == "lmstudio" else None
    engine_label = "LM Studio" if engine == "lmstudio" else "Ollama"
    cursor = 0
    while cursor < total:
        tag = tags[cursor]
        daemon_reachable = (
            linker.lmstudio_daemon_reachable() if engine == "lmstudio" else ollama_version() is not None
        )
        if not daemon_reachable:
            # Daemon died partway through the batch (e.g. crashed while
            # benchmarking a previous tag). Try to bring it back instead of
            # letting every remaining tag burn a full
            # DEFAULT_GENERATION_TIMEOUT_SECONDS per request before failing.
            if on_daemon_event is not None:
                on_daemon_event(
                    f"{engine_label} daemon isn't reachable - it likely crashed mid-run. "
                    "Attempting to restart it..."
                )
            if engine == "lmstudio":
                restart_failed = not linker.start_lmstudio_daemon()
            else:
                restart_failed = benchmark.start_ollama_daemon() is None
            if restart_failed:
                consecutive_daemon_failures += 1
                if consecutive_daemon_failures >= _MAX_CONSECUTIVE_DAEMON_FAILURES:
                    if on_daemon_event is not None:
                        on_daemon_event(
                            f"{engine_label} daemon won't come back after "
                            f"{consecutive_daemon_failures} attempts - stopping the "
                            f"remaining benchmark run ({total - cursor} tag(s) not attempted)."
                        )
                    break
                time.sleep(_DAEMON_RESTART_BACKOFF_SECONDS)
                continue
            consecutive_daemon_failures = 0
            if engine == "lmstudio":
                lmstudio_port = linker.lmstudio_server_port()
        cursor += 1
        if on_model_start is not None:
            on_model_start(tag, cursor, total)
        if engine == "lmstudio":
            entry = _evaluate_tag_once(
                tag, hardware, pack, speed_runs,
                engine="lmstudio",
                lmstudio_port=lmstudio_port,
                lmstudio_model=(lmstudio_models or {}).get(tag),
            )
        else:
            entry = _evaluate_tag_once(tag, hardware, pack, speed_runs)
        if (
            confirm_performance_timeout
            and engine == "ollama"
            and entry.get("outcome") == "transient_error"
            and entry.get("failure_reason") == FAILURE_REASON_GENERATION_TIMEOUT
        ):
            # Never uploaded on its own: the confirmation flow replaces this
            # first-attempt dict with the single final verdict below, so
            # exactly one event ever reaches the caller for this tag.
            entry = _confirm_generation_timeout(tag, hardware, pack, speed_runs)
        models.append(entry)
    if engine == "lmstudio":
        engine_version = (
            LMStudioAdapter(base_url=f"http://127.0.0.1:{lmstudio_port}").health().version
            if lmstudio_port is not None
            else None
        )
    else:
        engine_version = ollama_version()
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "small reproducible smoke evaluation; not a leaderboard",
        "pack": {
            "id": pack["pack_id"],
            "version": pack.get("pack_version"),
            "sha256": pack_sha256,
            "item_count": len(pack["items"]),
            "sources": pack.get("sources", []),
        },
        "environment": {
            "engine": engine,
            "engine_version": engine_version,
            "os": hardware.os_name,
            "architecture": platform.machine(),
            "ram_gb": round(hardware.ram_total_gb, 1),
            "vram_gb": (
                round(hardware.vram_total_gb, 1) if hardware.vram_total_gb is not None else None
            ),
            "unified_memory": hardware.unified_memory,
            "raw_hardware_names_stored": False,
        },
        "generation": pack["generation"],
        "models": models,
    }


def write_evidence(report: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
    except OSError as e:
        raise QualityEvaluationError(f"Could not write evidence to {path}: {e}") from e
