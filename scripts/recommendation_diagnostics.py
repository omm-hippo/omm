"""Explain recommendation regressions without changing publication thresholds."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re

from scripts.model_quality_gate import evaluate_artifact, selection_context_key


def context_hash(order: list[str], row: list[float]) -> str:
    values = selection_context_key(order, row)
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()


def training_provenance(order, training, holdout) -> dict:
    return {
        "schema_version": 1,
        "published_model_is_evaluated_candidate": True,
        "training_context_hashes": sorted({context_hash(order, row) for row in training}),
        "holdout_context_hashes": sorted({context_hash(order, row) for row in holdout}),
    }


def _engine(features: dict) -> str:
    return "lmstudio" if features.get("engine_lmstudio", 0) else "llamacpp" if features.get("engine_llamacpp", 0) else "ollama"


def _size_band(features: dict) -> str:
    size = features.get("param_count_b", 0)
    return "under_3B" if size < 3 else "3B_to_10B" if size < 10 else "10B_to_30B" if size < 30 else "30B_plus"


def build_report(candidate: dict, baseline: dict, training_X, training_y, holdout_X, holdout_y,
                 *, telemetry_audit: dict, fit_audit: dict | None = None) -> dict:
    order = candidate["feature_order"]
    provenance = training_provenance(order, training_X, holdout_X)
    old = baseline.get("training_provenance")
    prior_contexts = old.get("training_context_hashes") if isinstance(old, dict) else None
    comparable_history = isinstance(prior_contexts, list) and all(
        isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) for x in prior_contexts
    )
    overlap = sorted(set(prior_contexts or []) & set(provenance["holdout_context_hashes"])) if comparable_history else None
    groups = defaultdict(list)
    contexts = defaultdict(list)
    for index, row in enumerate(holdout_X):
        features = dict(zip(order, row))
        for dimension, label in (
            ("engine", _engine(features)), ("model_size", _size_band(features)),
            ("quant_bits", str(features.get("quant_bits"))),
            ("memory", f"ram_{features.get('ram_gb'):g}_vram_{features.get('vram_gb'):g}"),
        ):
            groups[(dimension, label)].append(index)
        contexts[context_hash(order, row)].append(index)

    def compare(indices):
        X = [holdout_X[i] for i in indices]
        y = [holdout_y[i] for i in indices]
        before = evaluate_artifact(baseline, X, y)
        after = evaluate_artifact(candidate, X, y)
        return {"rows": len(indices), "baseline": before, "candidate": after,
                "mae_change": after["mae"] - before["mae"]}

    slices = [{"dimension": dimension, "group": label, **compare(indices)}
              for (dimension, label), indices in sorted(groups.items())]
    context_reports = []
    tuning_names = ("context_length", "gpu_offload_ratio", "cpu_threads", "num_batch")
    for key, indices in contexts.items():
        features = dict(zip(order, holdout_X[indices[0]]))
        runtime_settings = {tuple(dict(zip(order, holdout_X[i])).get(name) for name in tuning_names) for i in indices}
        context_reports.append({
            "context_hash": key, "engine": _engine(features),
            "ram_gb": features.get("ram_gb"), "vram_gb": features.get("vram_gb"),
            "runtime_setting_count": len(runtime_settings), **compare(indices),
        })
    context_reports.sort(key=lambda row: row["mae_change"], reverse=True)
    engines = Counter(_engine(dict(zip(order, row))) for row in training_X)
    return {
        "schema_version": 1,
        "training_rows": len(training_X), "holdout_rows": len(holdout_X),
        "in_sample_diagnostics_not_validation": {
            "baseline": evaluate_artifact(baseline, training_X, training_y),
            "candidate": evaluate_artifact(candidate, training_X, training_y),
        },
        "training_by_engine": dict(sorted(engines.items())),
        "coverage_gaps": {
            "engines_without_training_rows": [name for name in ("ollama", "lmstudio") if not engines[name]],
            "known_unfit_examples": (fit_audit or {}).get("negative_examples", 0),
            "contexts_mixing_runtime_settings": sum(item["runtime_setting_count"] > 1 for item in context_reports),
        },
        "telemetry_audit": telemetry_audit,
        "fit_telemetry_audit": fit_audit or {},
        "baseline_holdout_exposure": {
            "status": "known_overlap" if overlap else "no_known_overlap" if comparable_history else "unknown",
            "overlapping_contexts": overlap,
            "note": "Older artifacts do not record training membership; a fair unseen-data comparison cannot be established from their metadata alone.",
        },
        "publication_contract": "Publish the evaluated candidate's exact trees; never refit on the holdout after passing.",
        "slices": slices,
        "worst_contexts": context_reports[:20],
        "collection_priorities": [
            {"context_hash": item["context_hash"], "engine": item["engine"],
             "ram_gb": item["ram_gb"], "vram_gb": item["vram_gb"],
             "reason": "Compare at least two models with identical runtime settings on this poorly predicted hardware group."}
            for item in context_reports[:5] if item["mae_change"] > 0
        ],
        "limits": [
            "These group comparisons locate observed errors; they do not establish why an individual measurement is slow.",
            "Do not lower release thresholds or force unsafe model loads to collect negative examples.",
        ],
    }
