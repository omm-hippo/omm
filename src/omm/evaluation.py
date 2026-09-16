"""Explain evaluation evidence without conflating a release gate with coverage.

The historical ``passed`` field is a regression/publication decision. It may be
true while a fit check was skipped. Derive coverage from the recorded metrics
and sample counts, including for older signed artifacts; never rewrite them.
"""
from __future__ import annotations

import math


METRICS = {
    "rmsle": ("Speed prediction error", "speed", False),
    "p90_absolute_percentage_error": ("Worst-case speed error (P90)", "speed", False),
    "top1_selection_accuracy": ("Model selection accuracy", "selection", True),
    "mean_normalized_regret": ("Average selection loss", "selection", False),
    "p90_normalized_regret": ("Worst-case selection loss (P90)", "selection", False),
    "fit_balanced_accuracy": ("Fit classification accuracy", "fit", True),
    "fit_false_positive_rate": ("Incorrectly predicted fits", "fit", False),
}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def describe_evaluation(evaluation: object) -> dict:
    """Return bounded, JSON-safe per-check verdicts and actionable evidence gaps."""
    report = evaluation if isinstance(evaluation, dict) else {}
    candidate = report.get("candidate")
    baseline = report.get("baseline")
    thresholds = report.get("thresholds")
    candidate = candidate if isinstance(candidate, dict) else {}
    baseline = baseline if isinstance(baseline, dict) else {}
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    checks = {}
    gaps = {}
    for metric, (label, group, higher_is_better) in METRICS.items():
        actual, previous = _number(candidate.get(metric)), _number(baseline.get(metric))
        if group == "speed":
            count, required = _count(candidate.get("rows")), 1
            limit_name = "max_rmsle_regression" if metric == "rmsle" else "max_p90_ape_regression"
        elif group == "selection":
            count = _count(candidate.get("selection_group_count"))
            required = _count(thresholds.get("min_selection_groups"))
            limit_name = "max_selection_metric_regression"
        else:
            count = _count(report.get("fit_negative_examples"))
            required = _count(thresholds.get("min_fit_negative_examples"))
            limit_name = "max_selection_metric_regression"
        tolerance = _number(thresholds.get(limit_name))
        reason = None
        if count is None or required is None or required < 1:
            reason = "Sample count or required minimum is not recorded."
        elif count < required:
            reason = f"Only {count} evidence sample(s); at least {required} required."
            gaps[group] = {
                "available": count, "required": required, "additional_needed": required - count,
                "sample_type": {
                    "fit": "known-unfit measurements",
                    "selection": "multi-model comparison groups",
                    "speed": "held-out timing measurements",
                }[group],
                "next_step": {
                    "fit": "Collect naturally observed, classified failures with memory protection enabled; do not force an out-of-memory condition.",
                    "selection": "Compare multiple models on the same hardware and request settings, across additional hardware contexts.",
                    "speed": "Add independent measured timings to the held-out evaluation set.",
                }[group],
            }
        elif actual is None or previous is None or tolerance is None or tolerance < 0:
            reason = "Comparable finite metrics or tolerance are missing."
        if reason:
            status = "insufficient_data"
        else:
            regressed = (
                actual > previous * (1 + tolerance) if group == "speed"
                else actual < previous - tolerance if higher_is_better
                else actual > previous + tolerance
            )
            status = "failed" if regressed else "passed"
            reason = "Regressed beyond the allowed tolerance." if regressed else "No regression beyond the allowed tolerance."
        checks[metric] = {
            "label": label, "group": group, "status": status, "reason": reason,
            "sample_count": count, "minimum_sample_count": required,
            "candidate_value": actual, "baseline_value": previous,
        }
    statuses = {check["status"] for check in checks.values()}
    status = "failed" if "failed" in statuses else "insufficient_data" if "insufficient_data" in statuses else "passed"
    return {
        "status": status,
        "fully_evaluated": "insufficient_data" not in statuses,
        "publication_gate_passed": report.get("passed") if isinstance(report.get("passed"), bool) else None,
        "checks": checks,
        "data_gaps": gaps,
    }
