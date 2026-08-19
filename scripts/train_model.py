"""CI-only training script (run by .github/workflows/train.yml).

Fetches benchmark telemetry from a self-hosted export endpoint or legacy
Firebase-compatible JSON, validates it, and
trains a small random-forest regressor predicting tokens/sec. Sparse data is
bootstrapped with synthetic rows derived from the bundled default rules. The
forest is exported as plain JSON (see omm.mltree for why: no pickle and no
runtime scikit-learn dependency for end users).

Not part of the omm package itself - requires scikit-learn, which is a
CI-only dependency (see requirements-train.txt), never shipped to users.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from sklearn.ensemble import RandomForestRegressor
from sklearn.tree import _tree

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omm.featurize import (  # noqa: E402
    FEATURE_ORDER,
    build_features,
    candidate_active_parameter_count_billions,
    candidate_parameter_count_billions,
    candidate_quant_bits,
    estimate_model_size_gb,
    parse_chip_score,
)
from omm.atomic import atomic_write_text, locked  # noqa: E402
from scripts.model_quality_gate import (  # noqa: E402
    InsufficientTelemetryError,
    compare_artifacts,
    selection_context_key,
    validate_artifact,
    validate_dataset,
)

TELEMETRY_URL = "http://127.0.0.1:8000/v1/benchmarks/export"
MIN_REAL_ROWS = 10
MAX_REAL_ROWS = 5000
REAL_BOOTSTRAP_WEIGHT = 12.0
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "published" / "recommend-model.json"

RAM_GRID = [4, 8, 16, 24, 32, 64, 128]
VRAM_GRID = [0, 4, 6, 8, 12, 16, 24, 32, 48, 64]
PARAMETER_GRID_B = [0.35, 0.5, 0.6, 1.0, 1.1, 1.5, 2.0, 3.0, 4.0, 7.0, 8.0, 13.0, 20.0, 27.0, 32.0, 70.0]
MOE_PARAMETER_GRID_B = [(8.0, 1.0), (14.0, 3.0), (30.0, 3.0), (35.0, 3.0)]
QUANT_GRID_BITS = [3.0, 4.0, 5.0, 6.0, 8.0, 16.0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the Localfit recommendation artifact.")
    parser.add_argument(
        "--telemetry-file",
        type=Path,
        action="append",
        default=[],
        help="Append a local JSON or JSONL benchmark file; repeatable.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not read Firebase; train only from supplied files and bootstrap rows.",
    )
    parser.add_argument(
        "--telemetry-url",
        default=os.environ.get("LOCALFIT_TELEMETRY_URL", TELEMETRY_URL),
        help="Firebase JSON URL or self-hosted /v1/benchmarks/export endpoint.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Artifact destination (defaults to published/recommend-model.json).",
    )
    parser.add_argument("--baseline", type=Path, help="Incumbent artifact used by the quality gate.")
    parser.add_argument(
        "--quality-gate",
        action="store_true",
        help="Validate telemetry and reject candidate regressions before publishing.",
    )
    parser.add_argument("--minimum-real-configurations", type=int, default=20)
    parser.add_argument("--maximum-rejection-rate", type=float, default=0.25)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument(
        "--minimum-fit-negative-examples",
        type=int,
        default=5,
        help=(
            "Minimum known-unfit (model_unfit/performance_unfit) telemetry rows "
            "required before fit_balanced_accuracy/fit_false_positive_rate can "
            "block a release; below this, a single row's prediction is too "
            "noisy to gate on."
        ),
    )
    parser.add_argument("--quality-report", type=Path, help="Optional JSON quality-gate report.")
    return parser.parse_args()


def is_firebase_realtime_database_json_url(url: str) -> bool:
    """Return whether *url* is an official Firebase RTDB JSON endpoint."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    is_firebase_host = hostname.endswith(".firebaseio.com") or hostname.endswith(
        ".firebasedatabase.app"
    )
    return is_firebase_host and parsed.path.endswith(".json")


def fetch_real_rows(url: str = TELEMETRY_URL) -> list[dict]:
    token = os.environ.get("LOCALFIT_ADMIN_TOKEN")
    # Firebase RTDB JSON endpoints are public-read in this workflow.  Avoid
    # sending a configured admin credential to that third-party URL.
    headers = (
        {"Authorization": f"Bearer {token}"}
        if token and not is_firebase_realtime_database_json_url(url)
        else {}
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Warning: couldn't fetch telemetry ({e}), treating as 0 real rows.")
        return []
    if isinstance(data, dict) and isinstance(data.get("benchmarks"), list):
        return [row for row in data["benchmarks"][-MAX_REAL_ROWS:] if isinstance(row, dict)]
    if isinstance(data, dict):
        return [row for row in list(data.values())[-MAX_REAL_ROWS:] if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data[-MAX_REAL_ROWS:] if isinstance(row, dict)]
    return []


def load_telemetry_file(path: Path) -> list[dict]:
    """Load Firebase-shaped JSON, a JSON list, or local benchmark JSONL."""
    try:
        raw = path.read_text()
    except OSError as error:
        raise ValueError(f"could not read {path}: {error}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        rows = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number} is not valid JSON") from error
            if isinstance(row, dict):
                rows.append(row)
        return rows
    if isinstance(payload, dict):
        if isinstance(payload.get("benchmarks"), list):
            return [row for row in payload["benchmarks"] if isinstance(row, dict)]
        # A single benchmark event is distinguished from Firebase's push-ID
        # mapping by the required measurement field.
        if "tokens_per_sec" in payload:
            return [payload]
        return [row for row in payload.values() if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise ValueError(f"{path} must contain a JSON object, list, or JSONL records")


def _bounded_number(value, minimum: float, maximum: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _direct_bounded_number(value, minimum: float, maximum: float) -> float | None:
    """Accept only JSON numeric values, never stringified model metadata."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _bounded_number(value, minimum, maximum)


#: v7 outcome enum (see docs/telemetry-v7.md). "success" carries a real
#: tokens_per_sec measurement, exactly like v1-v6. "model_unfit" and
#: "performance_unfit" are both negative fit-classification labels with no
#: speed measurement at all - the former from an explicit OOM/unsupported-
#: runtime error, the latter from two confirmed generation timeouts under a
#: healthy daemon. "transient_error" says nothing about fit and is excluded
#: from every dataset below.
V7_OUTCOMES = ("success", "model_unfit", "performance_unfit", "transient_error")

#: Rejection reasons meaning "this row was correctly routed elsewhere, not
#: that the data is malformed." validate_dataset() excludes these from its
#: rejection-rate gate for exactly that reason.
INTENTIONALLY_EXCLUDED_REASONS = frozenset({
    "model_unfit_excluded_from_regression",
    "performance_unfit_excluded_from_regression",
    "transient_error_excluded",
    "pressured_measurement_excluded",
    "unstable_measurement_excluded",
})


def _extract_features_and_reason(
    row: dict, *, require_speed: bool
) -> tuple[list[float] | None, float | None, str | None]:
    """Build a feature vector from a row's hardware/model/runtime metadata.

    Shared by the speed-regression path (`_real_row_to_sample`, always
    `require_speed=True`) and the fit-classification dataset
    (`_real_row_to_fit_sample`, `require_speed=False` for v7 model_unfit/
    performance_unfit rows, which by design carry no speed measurement at
    all).

    When `require_speed` is False, `tokens_per_sec`/sample-summary fields
    are never consulted, and the returned tokens_per_sec is always None.
    Every other check (runtime, model metadata, CPU metadata) is identical
    to the require_speed=True path, so a model_unfit/performance_unfit row
    is held to the same "do we actually know what this is" bar as a real
    measurement.
    """
    if not isinstance(row, dict):
        return None, None, "not_an_object"
    engine = row.get("engine") or "ollama"
    if engine not in ("ollama", "llama.cpp", "lmstudio"):
        return None, None, "unsupported_engine"
    benchmark_version = row.get("benchmark_version")
    if benchmark_version not in (None, 1, 2, 3, 4, 5, 6, 7, 8, 9):
        return None, None, "unsupported_schema"

    tokens_per_sec = _bounded_number(row.get("tokens_per_sec"), 0.0, 10_000.0)
    ram_gb = _bounded_number(row.get("ram_gb"), 1.0, 1024.0)
    vram_gb = _bounded_number(row.get("vram_gb"), 0.0, 512.0)
    gpu_tflops = _bounded_number(row.get("gpu_tflops"), 0.0, 1000.0)
    # "direct" = the explicit-metadata schema (v6+), as opposed
    # to the legacy name-parsing fallback used by v1-v5.
    is_direct = benchmark_version in (6, 7, 8, 9)
    if is_direct:
        required_runtime = (
            "runtime_profile", "context_length", "gpu_offload_percent", "cpu_threads", "num_batch",
        )
        if any(row.get(field) is None for field in required_runtime):
            return None, None, "missing_runtime_metadata"
        context_length = _direct_bounded_number(row.get("context_length"), 256.0, 131072.0)
        gpu_offload_percent = _direct_bounded_number(row.get("gpu_offload_percent"), 0.0, 100.0)
        cpu_threads = _direct_bounded_number(row.get("cpu_threads"), 1.0, 1024.0)
        num_batch = _direct_bounded_number(row.get("num_batch"), 1.0, 65536.0)
    else:
        context_length = _bounded_number(row.get("context_length", 2048), 256.0, 131072.0)
        gpu_offload_percent = _bounded_number(
            row.get("gpu_offload_percent", 100 if (vram_gb or 0) > 0 else 0), 0.0, 100.0
        )
        cpu_threads = _bounded_number(row.get("cpu_threads", 0), 0.0, 1024.0)
        num_batch = _bounded_number(row.get("num_batch", 0), 0.0, 65536.0)
    if (require_speed and tokens_per_sec is None) or ram_gb is None:
        return None, None, "invalid_measurement"
    if None in (context_length, gpu_offload_percent, cpu_threads, num_batch):
        return None, None, "invalid_runtime"
    if require_speed and benchmark_version in (4, 5, 6, 7, 8, 9):
        if is_direct and any(
            row.get(field) is None
            for field in ("sample_count", "tokens_per_sec_min", "tokens_per_sec_max")
        ):
            return None, None, "missing_sample_summary"
        number_parser = _direct_bounded_number if is_direct else _bounded_number
        sample_count = number_parser(row.get("sample_count", 1), 1.0, 10.0)
        sample_min = number_parser(
            row.get("tokens_per_sec_min", tokens_per_sec), 0.0, 10_000.0
        )
        sample_max = number_parser(
            row.get("tokens_per_sec_max", tokens_per_sec), 0.0, 10_000.0
        )
        if (
            sample_count is None
            or not sample_count.is_integer()
            or sample_min is None
            or sample_max is None
            or not sample_min <= tokens_per_sec <= sample_max
            or (is_direct and sample_count < 3)
        ):
            return None, None, "invalid_samples"

    if benchmark_version == 9:
        required_v9 = (
            "measurement_profile",
            "measurement_quality",
            "ram_available_before_gb",
            "ram_available_min_gb",
            "ram_available_after_gb",
            "memory_pressure_observed",
            "tokens_per_sec_mad_ratio",
            "memory_estimate_source",
            "memory_estimate_confidence",
            "estimated_mapped_weights_gb",
            "estimated_committed_ram_gb",
            "estimated_required_vram_gb",
        )
        if any(row.get(field) is None for field in required_v9):
            return None, None, "missing_measurement_metadata"
        before = _direct_bounded_number(row.get("ram_available_before_gb"), 0.0, 4096.0)
        minimum = _direct_bounded_number(row.get("ram_available_min_gb"), 0.0, 4096.0)
        after = _direct_bounded_number(row.get("ram_available_after_gb"), 0.0, 4096.0)
        mad_ratio = _direct_bounded_number(row.get("tokens_per_sec_mad_ratio"), 0.0, 10.0)
        estimates = tuple(
            _direct_bounded_number(row.get(field), 0.0, 4096.0)
            for field in (
                "estimated_mapped_weights_gb",
                "estimated_committed_ram_gb",
                "estimated_required_vram_gb",
            )
        )
        quality = row.get("measurement_quality")
        pressure = row.get("memory_pressure_observed")
        if (
            row.get("measurement_profile") != "contribute-v1"
            or context_length != 1024
            or num_batch != 128
            or quality not in ("clean", "pressured", "unstable")
            or not isinstance(pressure, bool)
            or row.get("memory_estimate_source") not in ("gguf_header", "profile_fallback")
            or row.get("memory_estimate_confidence") not in ("low", "medium", "high")
            or None in (before, minimum, after, mad_ratio, *estimates)
            or minimum > min(before, after)
            or (pressure and quality != "pressured")
            or (not pressure and quality == "pressured")
            or (not pressure and quality == "clean" and mad_ratio > 0.15)
            or (not pressure and quality == "unstable" and mad_ratio <= 0.15)
        ):
            return None, None, "invalid_measurement_metadata"

    if is_direct:
        required_model_metadata = (
            "parameter_count_b", "active_parameter_count_b", "quant_bits", "engine_version", "client_version",
        )
        if any(row.get(field) is None for field in required_model_metadata):
            return None, None, "missing_model_metadata"
        param_count_b = _direct_bounded_number(row.get("parameter_count_b"), 0.0, 10_000.0)
        active_param_count_b = _direct_bounded_number(
            row.get("active_parameter_count_b"), 0.0, 10_000.0
        )
        quant_bits = _direct_bounded_number(row.get("quant_bits"), 0.5, 32.0)
        if (
            param_count_b is None
            or active_param_count_b is None
            or quant_bits is None
            or param_count_b <= 0
            or active_param_count_b <= 0
            or active_param_count_b > param_count_b
            or not isinstance(row.get("runtime_profile"), str)
            or not row["runtime_profile"].strip()
            or not isinstance(row.get("engine_version"), str)
            or not row["engine_version"].strip()
            or not isinstance(row.get("client_version"), str)
            or not row["client_version"].strip()
            or len(row["engine_version"]) > 100
            or len(row["client_version"]) > 100
        ):
            return None, None, "invalid_model_metadata"
        size_bytes = _bounded_number(row.get("model_size_bytes"), 1.0, 10**15)
        model_size_gb = (
            size_bytes / (1024**3)
            if size_bytes is not None
            else param_count_b * quant_bits / 8.0 * 1.1
        )
    else:
        installed_name = str(row.get("model_installed") or "")
        repo_name = str(row.get("model_repo_id") or "").rsplit("/", 1)[-1]
        text = f"{installed_name} {repo_name}"
        candidate = {
            "name": installed_name,
            "filename": installed_name,
            "repo_id": row.get("model_repo_id"),
            "size_bytes": row.get("model_size_bytes"),
        }
        param_count_b = candidate_parameter_count_billions(candidate)
        active_param_count_b = candidate_active_parameter_count_billions(candidate)
        quant_bits = candidate_quant_bits(candidate)
        model_size_gb = estimate_model_size_gb(text, row.get("model_size_bytes"))
        if param_count_b is None or quant_bits is None or model_size_gb is None:
            return None, None, "unparseable_model"

    if benchmark_version in (8, 9):
        cpu_score = _direct_bounded_number(row.get("cpu_score"), 0.0, 99_999.0)
        cpu_tier = _direct_bounded_number(row.get("cpu_tier"), 0.0, 10.0)
        if cpu_score is None or cpu_tier is None:
            return None, None, "missing_cpu_metadata"
        gpu_score = _direct_bounded_number(row.get("gpu_score"), 0.0, 99_999.0) or 0.0
        gpu_tier = _direct_bounded_number(row.get("gpu_tier"), 0.0, 10.0) or 0.0
    elif is_direct:
        cpu_model = row.get("cpu_model")
        if not isinstance(cpu_model, str) or not cpu_model.strip():
            return None, None, "missing_cpu_metadata"
        cpu_score, cpu_tier = parse_chip_score(cpu_model)
        gpu_score, gpu_tier = 0.0, 0.0
    else:
        # Pre-v6 telemetry never collected cpu_model at all, so this branch
        # used to silently score every such row's hardware as (0.0, 0.0) -
        # indistinguishable from every OTHER pre-v6 row regardless of their
        # real (and very different) speeds. Once enough real telemetry
        # accumulated, that collapsed bucket's variance alone was enough to
        # regress p90_absolute_percentage_error and block every retrain
        # (2026-08-07 incident). Excluded rather than guessed at.
        return None, None, "no_hardware_identity_pre_v6_schema"
    features = build_features(
        ram_gb=ram_gb,
        vram_gb=vram_gb,
        unified_memory=bool(row.get("unified_memory")),
        param_count_b=param_count_b,
        quant_bits=quant_bits,
        model_size_gb=model_size_gb,
        gpu_tflops=gpu_tflops or 0.0,
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        gpu_score=gpu_score,
        gpu_tier=gpu_tier,
        context_length=context_length,
        gpu_offload_ratio=gpu_offload_percent / 100.0,
        cpu_threads=cpu_threads,
        num_batch=num_batch,
        active_param_count_b=active_param_count_b,
        engine=engine,
    )
    return features, (tokens_per_sec if require_speed else None), None


def _real_row_to_sample(row: dict) -> tuple[tuple[list[float], float] | None, str | None]:
    """Speed-regression sample. v7 rows require an explicit `outcome`:
    only "success" carries a real measurement; "model_unfit",
    "performance_unfit", and "transient_error" are excluded here by design
    (see `real_rows_to_fit_training_data_with_audit` for where model_unfit/
    performance_unfit go instead) - never coerced into a fake
    tokens_per_sec=0 regression row.
    """
    if isinstance(row, dict) and row.get("benchmark_version") in (7, 8, 9):
        outcome = row.get("outcome")
        if outcome not in V7_OUTCOMES:
            return None, "invalid_outcome"
        if outcome == "transient_error":
            return None, "transient_error_excluded"
        if outcome == "model_unfit":
            return None, "model_unfit_excluded_from_regression"
        if outcome == "performance_unfit":
            return None, "performance_unfit_excluded_from_regression"
        if row.get("benchmark_version") == 9:
            quality = row.get("measurement_quality")
            if quality == "pressured":
                return None, "pressured_measurement_excluded"
            if quality == "unstable":
                return None, "unstable_measurement_excluded"
            if quality != "clean":
                return None, "invalid_measurement_quality"
    features, tokens_per_sec, reason = _extract_features_and_reason(row, require_speed=True)
    if features is None:
        return None, reason
    return (features, tokens_per_sec), None


def _real_row_to_fit_sample(row: dict) -> tuple[tuple[list[float], bool] | None, str | None]:
    """Fit/unfit classification sample, independent of speed regression.

    Explicit v7 outcome takes priority: "model_unfit" and
    "performance_unfit" both contribute a negative (fit=False) example
    built from best-effort model/runtime metadata (no speed data involved -
    a confirmed-timeout row is exactly as speed-less as an OOM row),
    "transient_error" is excluded entirely (it says nothing about fit), and
    "success" contributes a positive example.

    v6 rows have no `outcome` field yet, but do carry a real cpu_model -
    so a valid v6 row is treated as an implicit success (fit=True), same
    backward-compatibility path as the speed-regression dataset. Pre-v6
    rows have no cpu_model at all and are excluded entirely (see
    `_extract_features_and_reason`'s no_hardware_identity_pre_v6_schema),
    not assumed positive - old data with zero hardware signal shouldn't
    silently vote either way.
    """
    if not isinstance(row, dict):
        return None, "not_an_object"
    if row.get("benchmark_version") in (7, 8, 9):
        outcome = row.get("outcome")
        if outcome not in V7_OUTCOMES:
            return None, "invalid_outcome"
        if outcome == "transient_error":
            return None, "transient_error_excluded"
        if outcome in ("model_unfit", "performance_unfit"):
            features, _tokens_per_sec, reason = _extract_features_and_reason(row, require_speed=False)
            if features is None:
                return None, reason
            return (features, False), None
    features, _tokens_per_sec, reason = _extract_features_and_reason(row, require_speed=True)
    if features is None:
        return None, reason
    return (features, True), None


def real_rows_to_training_data_with_audit(
    rows: list[dict],
) -> tuple[list[list[float]], list[float], dict]:
    groups: dict[tuple[float, ...], list[float]] = {}
    rejections: dict[str, int] = {}
    valid_rows = 0
    samples_used = 0
    samples_capped = 0
    direct_v6_groups: set[tuple[float, ...]] = set()
    direct_v7_groups: set[tuple[float, ...]] = set()
    direct_v8_groups: set[tuple[float, ...]] = set()
    direct_v9_groups: set[tuple[float, ...]] = set()
    for row in rows:
        sample, reason = _real_row_to_sample(row)
        if sample is None:
            rejections[reason or "unknown"] = rejections.get(reason or "unknown", 0) + 1
            continue
        valid_rows += 1
        features, tokens_per_sec = sample
        # Collapse repeated measurements of the same configuration to a
        # median. This makes community retraining useful without allowing one
        # noisy client or a burst of duplicate uploads to dominate the fit.
        group_key = tuple(round(value, 3) for value in features)
        benchmark_version = row.get("benchmark_version")
        if benchmark_version == 6:
            # Reaching this point means _real_row_to_sample accepted the
            # explicit v6 model, runtime, CPU, and sample metadata.
            direct_v6_groups.add(group_key)
        elif benchmark_version == 7:
            # Same explicit-metadata bar, via the v7 outcome="success" path.
            direct_v7_groups.add(group_key)
        elif benchmark_version == 8:
            # Same bar again, via v8's cpu_score/gpu_score in place of
            # cpu_model.
            direct_v8_groups.add(group_key)
        elif benchmark_version == 9:
            direct_v9_groups.add(group_key)
        samples = groups.setdefault(group_key, [])
        if len(samples) < 50:
            samples.append(tokens_per_sec)
            samples_used += 1
        else:
            samples_capped += 1

    X = [list(features) for features in groups]
    y = [statistics.median(samples) for samples in groups.values()]
    audit = {
        "raw_rows": len(rows),
        "valid_rows": valid_rows,
        "rejected_rows": len(rows) - valid_rows,
        "samples_used": samples_used,
        "samples_capped": samples_capped,
        "unique_configurations": len(groups),
        "direct_v6_unique_configurations": len(direct_v6_groups),
        "direct_v7_unique_configurations": len(direct_v7_groups),
        "direct_v8_unique_configurations": len(direct_v8_groups),
        "direct_v9_unique_configurations": len(direct_v9_groups),
        # Union, not sum: a configuration benchmarked under multiple direct schemas
        # (same hardware/model/runtime feature vector, just a newer client)
        # is one real training example, not three. This is the count the
        # quality gate's min_unique_configurations threshold should compare
        # against - see validate_dataset(). The per-version counts above
        # stay purely diagnostic.
        "direct_unique_configurations": len(
            direct_v6_groups | direct_v7_groups | direct_v8_groups | direct_v9_groups
        ),
        "duplicates_collapsed": samples_used - len(groups),
        "rejections": dict(sorted(rejections.items())),
    }
    return X, y, audit


def real_rows_to_training_data(rows: list[dict]) -> tuple[list[list[float]], list[float]]:
    X, y, _audit = real_rows_to_training_data_with_audit(rows)
    return X, y


def real_rows_to_fit_training_data_with_audit(
    rows: list[dict],
) -> tuple[list[list[float]], list[bool], dict]:
    """Build the fit/unfit classification dataset - separate from the speed
    regression dataset above by design (see docs/telemetry-v7.md, "why two
    datasets"). Positive examples come from any successful measurement
    (v1-v7); negative examples come from explicit v7 model_unfit rows (an
    outright OOM/unsupported-runtime failure) and performance_unfit rows
    (two confirmed generation timeouts under a healthy daemon). Both are
    equally valid "this configuration does not work" evidence for the
    classifier even though only model_unfit reflects a hardware capacity
    failure. transient_error rows are excluded entirely: a temporary hiccup
    says nothing about whether the model fits.
    """
    fit_X: list[list[float]] = []
    fit_y: list[bool] = []
    rejections: dict[str, int] = {}
    valid_rows = 0
    positive_examples = 0
    negative_examples = 0
    for row in rows:
        sample, reason = _real_row_to_fit_sample(row)
        if sample is None:
            rejections[reason or "unknown"] = rejections.get(reason or "unknown", 0) + 1
            continue
        valid_rows += 1
        features, is_fit = sample
        fit_X.append(list(features))
        fit_y.append(is_fit)
        if is_fit:
            positive_examples += 1
        else:
            negative_examples += 1
    audit = {
        "raw_rows": len(rows),
        "valid_rows": valid_rows,
        "rejected_rows": len(rows) - valid_rows,
        "positive_examples": positive_examples,
        "negative_examples": negative_examples,
        "rejections": dict(sorted(rejections.items())),
    }
    return fit_X, fit_y, audit


def stable_holdout_split(
    X: list[list[float]], y: list[float], holdout_fraction: float
) -> tuple[list[list[float]], list[float], list[list[float]], list[float]]:
    """Split complete selection contexts without leaking sibling candidates."""
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be between 0 and 1")
    if len(X) != len(y):
        raise ValueError("X and y must have the same number of rows")
    groups: dict[tuple[float, ...], list[tuple[list[float], float]]] = {}
    for features, target in zip(X, y):
        groups.setdefault(selection_context_key(FEATURE_ORDER, features), []).append((features, target))
    if len(groups) < 2:
        raise ValueError("quality gate requires at least two selection contexts")
    ordered_groups = sorted(
        (
            sorted(
                group,
                key=lambda sample: json.dumps(sample, separators=(",", ":"), ensure_ascii=True),
            )
            for group in groups.values()
        ),
        key=lambda group: hashlib.sha256(
            json.dumps(selection_context_key(FEATURE_ORDER, group[0][0]), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    )
    holdout_target = max(1, int(math.ceil(len(X) * holdout_fraction)))
    holdout = []
    holdout_group_count = 0
    for group in ordered_groups[:-1]:
        if len(holdout) >= holdout_target:
            break
        holdout.extend(group)
        holdout_group_count += 1
    training = [sample for group in ordered_groups[holdout_group_count:] for sample in group]
    return (
        [features for features, _target in training],
        [target for _features, target in training],
        [features for features, _target in holdout],
        [target for _features, target in holdout],
    )


def synthetic_rows_from_rules() -> tuple[list[list[float]], list[float]]:
    """Build a broad, deterministic bootstrap grid.

    The first version only sampled the three bundled model rules (1.1B, 7B,
    and 8B). A tree trained from those points assigned the same throughput to
    nearly every sub-2B model. The denser parameter/quantization grid keeps the
    cold-start estimator honest about size differences while real telemetry
    remains the higher-weight source of truth.
    """
    X, y = [], []
    architectures = [(parameters, parameters) for parameters in PARAMETER_GRID_B]
    architectures.extend(MOE_PARAMETER_GRID_B)
    for param_count_b, active_param_count_b in architectures:
        for quant_bits in QUANT_GRID_BITS:
            model_size_gb = param_count_b * quant_bits / 8.0 * 1.1
            for ram_gb in RAM_GRID:
                for vram_gb in VRAM_GRID:
                    unified = vram_gb == ram_gb and vram_gb > 0
                    required_gb = model_size_gb * 1.2
                    available_gb = max(ram_gb * 0.8, vram_gb * 0.9)
                    meets = available_gb >= required_gb
                    if unified:
                        gpu_offload_ratio = 1.0
                    elif vram_gb <= 0:
                        gpu_offload_ratio = 0.0
                    elif model_size_gb <= vram_gb * 0.85:
                        gpu_offload_ratio = 1.0
                    else:
                        gpu_offload_ratio = max(0.1, min(0.9, vram_gb * 0.8 / model_size_gb))
                    accelerator_factor = 0.35 + 0.65 * gpu_offload_ratio
                    baseline_speed = 100.0 / (
                        active_param_count_b * math.sqrt(quant_bits / 4.0)
                    )
                    speed = baseline_speed * accelerator_factor if meets else 0.0
                    for context_length, context_factor in ((2048, 1.0), (4096, 0.97), (8192, 0.92)):
                        X.append(
                            build_features(
                                ram_gb=ram_gb,
                                vram_gb=vram_gb,
                                unified_memory=unified,
                                param_count_b=param_count_b,
                                quant_bits=quant_bits,
                                model_size_gb=model_size_gb,
                                context_length=context_length,
                                gpu_offload_ratio=gpu_offload_ratio,
                                cpu_threads=8,
                                num_batch=512 if gpu_offload_ratio >= 0.8 else 256 if gpu_offload_ratio > 0 else 128,
                                active_param_count_b=active_param_count_b,
                            )
                        )
                        y.append(speed * context_factor)
    return X, y


def export_node(tree, node_id: int) -> dict:
    if tree.children_left[node_id] == _tree.TREE_LEAF:
        return {"leaf": True, "value": float(tree.value[node_id][0][0])}
    return {
        "feature": int(tree.feature[node_id]),
        "threshold": float(tree.threshold[node_id]),
        "left": export_node(tree, tree.children_left[node_id]),
        "right": export_node(tree, tree.children_right[node_id]),
    }


def train_artifact(
    X: list[list[float]],
    y: list[float],
    *,
    sample_weight: list[float] | None,
    training_mode: str,
    bootstrap_method: str | None,
    real_rows: list[dict],
    telemetry_audit: dict,
    input_sources: list[str],
    evaluation: dict | None,
) -> dict:
    model = RandomForestRegressor(
        n_estimators=64,
        max_depth=8,
        min_samples_leaf=2,
        random_state=0,
        n_jobs=-1,
    )
    model.fit(X, y, sample_weight=sample_weight)
    candidates = load_candidates()
    return {
        "model_version": 4,
        "feature_schema_version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_order": FEATURE_ORDER,
        "training_mode": training_mode,
        "bootstrap_method": bootstrap_method,
        "raw_real_row_count": len(real_rows),
        "real_row_count": telemetry_audit["unique_configurations"],
        "training_row_count": len(X),
        "telemetry_audit": telemetry_audit,
        "telemetry_sources": input_sources,
        "evaluation": evaluation,
        "trees": [export_node(estimator.tree_, 0) for estimator in model.estimators_],
        "candidates": candidates,
    }


def load_candidates() -> list[dict]:
    candidates_path = Path(__file__).resolve().parent.parent / "published" / "candidates.json"
    if candidates_path.exists():
        return json.loads(candidates_path.read_text())
    print("Warning: no published/candidates.json found, falling back to curated index only.")
    from omm.hub import CURATED_INDEX

    return [
        {"name": name, "repo_id": repo_id, "filename": filename, "description": ""}
        for name, (repo_id, filename) in CURATED_INDEX.items()
    ]


def main() -> None:
    args = parse_args()
    real_rows = [] if args.offline else fetch_real_rows(args.telemetry_url)
    input_sources = [] if args.offline else [
        "firebase_legacy"
        if is_firebase_realtime_database_json_url(args.telemetry_url)
        else "self_hosted"
    ]
    for telemetry_path in args.telemetry_file:
        file_rows = load_telemetry_file(telemetry_path)
        real_rows.extend(file_rows)
        input_sources.append("local_file")
        print(f"Loaded {len(file_rows)} local telemetry row(s) from {telemetry_path}.")
    real_rows = real_rows[-MAX_REAL_ROWS:]
    real_X, real_y, telemetry_audit = real_rows_to_training_data_with_audit(real_rows)
    print(
        f"Fetched {telemetry_audit['valid_rows']} valid telemetry rows "
        f"({telemetry_audit['raw_rows']} raw, {len(real_X)} unique configurations, "
        f"{telemetry_audit['rejected_rows']} rejected)."
    )

    if args.quality_gate:
        if args.baseline is None:
            raise ValueError("--baseline is required with --quality-gate")
        # Validate before constructing a candidate or touching the destination.
        try:
            validate_dataset(
                telemetry_audit,
                min_unique_configurations=args.minimum_real_configurations,
                max_rejection_rate=args.maximum_rejection_rate,
            )
        except InsufficientTelemetryError as error:
            # The telemetry corpus hasn't grown enough yet, not a code bug.
            # Republish the baseline unchanged rather than failing CI.
            try:
                baseline_text = args.baseline.read_text()
            except OSError as read_error:
                raise ValueError(
                    f"could not read baseline artifact {args.baseline}: {read_error}"
                ) from read_error
            print(f"Quality gate: {error}. Keeping current model unchanged.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with locked(args.output):
                atomic_write_text(args.output, baseline_text)
            if args.quality_report:
                args.quality_report.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    args.quality_report,
                    json.dumps(
                        {
                            "passed": False,
                            "skipped": True,
                            "reason": str(error),
                            "telemetry_audit": telemetry_audit,
                        },
                        indent=2,
                    )
                    + "\n",
                )
            return
        try:
            baseline_text = args.baseline.read_text()
            baseline = json.loads(baseline_text)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read baseline artifact {args.baseline}: {error}") from error
        validate_artifact(baseline, FEATURE_ORDER)
        train_X, train_y, holdout_X, holdout_y = stable_holdout_split(
            real_X, real_y, args.holdout_fraction
        )
        candidate = train_artifact(
            train_X,
            train_y,
            sample_weight=None,
            training_mode="telemetry",
            bootstrap_method=None,
            real_rows=real_rows,
            telemetry_audit=telemetry_audit,
            input_sources=input_sources,
            evaluation=None,
        )
        fit_X, fit_y, fit_audit = real_rows_to_fit_training_data_with_audit(real_rows)
        # Evaluation-only: the regressor never trains on these (it only ever
        # fits on real_X/real_y), so there is no train/holdout leakage risk
        # in scoring the whole fit dataset here.
        fit_examples = list(zip(fit_X, fit_y)) if fit_y else None
        evaluation = compare_artifacts(
            candidate, baseline, holdout_X, holdout_y,
            min_fit_negative_examples=args.minimum_fit_negative_examples,
            fit_examples=fit_examples,
        )
        evaluation.update(
            {
                "holdout_fraction": args.holdout_fraction,
                "training_rows": len(train_X),
                "holdout_rows": len(holdout_X),
                "fit_telemetry_audit": fit_audit,
            }
        )
        selection_group_count = evaluation.get("candidate", {}).get("selection_group_count")
        min_selection_groups = evaluation.get("thresholds", {}).get("min_selection_groups")
        insufficient_selection_groups = (
            selection_group_count is not None
            and min_selection_groups is not None
            and selection_group_count < min_selection_groups
        )
        if insufficient_selection_groups:
            evaluation["skipped"] = True
        if args.quality_report:
            args.quality_report.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(args.quality_report, json.dumps(evaluation, indent=2) + "\n")
        if insufficient_selection_groups:
            # Too few distinct real configurations to compare candidate vs.
            # baseline on ANY metric in this holdout, not just the
            # selection-ranking ones - same "come back with more telemetry"
            # treatment as the pre-training volume check above, not a bug.
            # `evaluation["failures"]` can also hold real regression
            # failures found in this same run (compare_artifacts appends
            # every reason to one list, regardless of cause) - print all of
            # them, not just the selection-group one, or a candidate that
            # both lacks data AND genuinely regressed would log as pure
            # "not enough data" and hide the regression from CI output.
            print(
                f"Quality gate: only {selection_group_count} selection group(s) in the "
                f"holdout, need {min_selection_groups}. Keeping current model unchanged. "
                "Full evaluation: " + "; ".join(evaluation["failures"])
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with locked(args.output):
                atomic_write_text(args.output, baseline_text)
            return
        if not evaluation["passed"]:
            # Candidate underperformed the baseline on real metrics - the gate
            # is working as intended, not a CI failure. Same "keep current
            # model unchanged" treatment as the data-volume skip paths above.
            print(
                "Quality gate: candidate rejected ("
                + "; ".join(evaluation["failures"])
                + "). Keeping current model unchanged."
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with locked(args.output):
                atomic_write_text(args.output, baseline_text)
            return
        artifact = train_artifact(
            real_X,
            real_y,
            sample_weight=None,
            training_mode="telemetry",
            bootstrap_method=None,
            real_rows=real_rows,
            telemetry_audit=telemetry_audit,
            input_sources=input_sources,
            evaluation=evaluation,
        )
    elif len(real_X) < MIN_REAL_ROWS:
        synth_X, synth_y = synthetic_rows_from_rules()
        print(f"Below {MIN_REAL_ROWS}-row threshold, adding {len(synth_X)} synthetic rows.")
        X, y = synth_X + real_X, synth_y + real_y
        sample_weight = [1.0] * len(synth_X) + [REAL_BOOTSTRAP_WEIGHT] * len(real_X)
        training_mode = "hybrid_bootstrap"
        artifact = train_artifact(
            X, y, sample_weight=sample_weight, training_mode=training_mode,
            bootstrap_method="dense_moe_parameter_quantization_grid_v2", real_rows=real_rows,
            telemetry_audit=telemetry_audit, input_sources=input_sources, evaluation=None,
        )
    else:
        X, y = real_X, real_y
        training_mode = "telemetry"
        artifact = train_artifact(
            X, y, sample_weight=None, training_mode=training_mode, bootstrap_method=None,
            real_rows=real_rows, telemetry_audit=telemetry_audit, input_sources=input_sources,
            evaluation=None,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with locked(args.output):
        atomic_write_text(args.output, json.dumps(artifact, indent=2) + "\n")
    print(
        f"Wrote {args.output} ({len(artifact['candidates'])} candidates, "
        f"{artifact['training_row_count']} training rows)"
    )


if __name__ == "__main__":
    main()
