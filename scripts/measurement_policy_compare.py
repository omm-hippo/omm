"""Compare speed-target aggregation policies on one offline telemetry snapshot.

Runs the *same* training-data pipeline (`train_model.real_rows_to_training_data_with_audit`:
row validation, physical ceiling, per-configuration collapse, statistical
outlier pass) twice over the same local JSON/JSONL file:

* ``old_median`` - each configuration's target is the plain median of all its
  accepted speeds (the policy before `scripts/measurement_balance.py`);
* ``repetition_balance`` - the current policy (`balanced_speed`).

It reports row retention, per-configuration target changes, and - when
scikit-learn is installed - the prediction and ranking changes of two forests
trained with the same hyperparameters on a fixed candidate set. Output JSON is
deterministic for a given input (sorted keys, rounded floats, no timestamps).

Nothing is downloaded or uploaded. Configurations are identified by a short
hash of their rounded feature vector, never by raw rows, so the report can be
shared without exposing individual submissions. Only real-row configurations
are used (no synthetic prior), so predictions here are a policy comparison,
not the published model's output.

Usage::

    python scripts/measurement_policy_compare.py --telemetry-file SNAPSHOT.json \
        [--candidates CANDIDATES.json] [--output REPORT.json]

``CANDIDATES.json`` is a list of ``{"id": str, "group": str, "features": [...]}``
(features in `FEATURE_ORDER`); ranks are computed within each ``group``.
Without it, every configuration in the snapshot is a candidate in one group.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import statistics
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from scripts import measurement_balance  # noqa: E402

POLICIES = ("old_median", "repetition_balance")
_DIGITS = 6


def _round(value: float) -> float:
    return round(float(value), _DIGITS)


def config_id(features: Sequence[float]) -> str:
    rounded = [round(float(value), 3) for value in features]
    rounded = [0.0 if value == 0 else value for value in rounded]
    digest = hashlib.sha256(json.dumps(rounded, separators=(",", ":")).encode()).hexdigest()
    return digest[:16]


def _plain_median(samples: Sequence[float]) -> tuple[float, int]:
    if not samples or any(not math.isfinite(x) or x <= 0 for x in samples):
        raise ValueError("speed samples must be non-empty, positive and finite")
    return statistics.median(samples), len(samples)


@contextlib.contextmanager
def _policy(name: str) -> Iterator[None]:
    """Swap the aggregator the training pipeline imports at call time."""
    original = measurement_balance.balanced_speed
    if name == "old_median":
        measurement_balance.balanced_speed = _plain_median
    elif name != "repetition_balance":
        raise ValueError(f"unknown policy {name!r}")
    try:
        yield
    finally:
        measurement_balance.balanced_speed = original


def build_dataset(rows: list[dict], policy: str) -> tuple[dict[str, list[float]], dict[str, float], dict]:
    from scripts import train_model

    with _policy(policy):
        X, y, audit = train_model.real_rows_to_training_data_with_audit(rows)
    features = {config_id(row): list(row) for row in X}
    targets = {config_id(row): target for row, target in zip(X, y)}
    return features, targets, audit


def _retention(audit: dict) -> dict:
    raw = audit["raw_rows"]
    return {
        "raw_rows": raw,
        "rows_retained": audit["valid_rows"],
        "row_retention_rate": _round(audit["valid_rows"] / raw) if raw else None,
        "configurations": audit["unique_configurations"],
        "outlier_rows_dropped": audit["speed_outliers"].get("dropped_rows", 0),
        "outlier_configurations_dropped": audit["speed_outliers"].get("dropped_configurations", 0),
        "rejections": audit["rejections"],
    }


def _target_changes(old: dict[str, float], new: dict[str, float]) -> dict:
    shared = sorted(set(old) & set(new))
    changes = []
    for key in shared:
        delta = new[key] - old[key]
        if abs(delta) > 1e-9:
            changes.append({
                "config": key,
                "old_target": _round(old[key]),
                "new_target": _round(new[key]),
                "delta": _round(delta),
                "relative_change": _round(delta / old[key]),
            })
    return {
        "shared_configurations": len(shared),
        "only_in_old_median": sorted(set(old) - set(new)),
        "only_in_repetition_balance": sorted(set(new) - set(old)),
        "changed_configurations": len(changes),
        "max_abs_relative_change": max((abs(c["relative_change"]) for c in changes), default=0.0),
        "changes": changes,
    }


def _load_candidates(path: Path | None, features: dict[str, list[float]]) -> list[dict]:
    if path is None:
        return [{"id": key, "group": "all", "features": features[key]} for key in sorted(features)]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("candidates file must be a JSON list")
    candidates = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or not isinstance(item.get("features"), list):
            raise ValueError(f"candidate {index} needs a features list")
        candidates.append({
            "id": str(item.get("id", index)),
            "group": str(item.get("group", "all")),
            "features": [float(value) for value in item["features"]],
        })
    return candidates


def _ranks(candidates: list[dict], predictions: list[float]) -> list[int]:
    ranks = [0] * len(candidates)
    groups: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(candidate["group"], []).append(index)
    for members in groups.values():
        ordered = sorted(members, key=lambda i: (-round(predictions[i], _DIGITS), candidates[i]["id"]))
        for rank, index in enumerate(ordered, start=1):
            ranks[index] = rank
    return ranks


def _predict(features: dict[str, list[float]], targets: dict[str, float], rows: list[list[float]]) -> list[float]:
    from sklearn.ensemble import RandomForestRegressor

    keys = sorted(features)
    # Same hyperparameters as train_model.train_artifact; n_jobs=1 so the
    # result does not depend on thread scheduling.
    model = RandomForestRegressor(n_estimators=64, max_depth=8, min_samples_leaf=2, random_state=0, n_jobs=1)
    model.fit([features[key] for key in keys], [targets[key] for key in keys])
    return [float(value) for value in model.predict(rows)]


def _prediction_changes(datasets: dict, candidates: list[dict]) -> dict:
    try:
        import sklearn  # noqa: F401
    except ImportError:
        return {"status": "skipped", "reason": "scikit-learn is not installed"}
    if not candidates:
        return {"status": "skipped", "reason": "no candidates"}
    for policy in POLICIES:
        if len(datasets[policy][0]) < 2:
            return {"status": "skipped", "reason": f"{policy} has fewer than 2 configurations"}
    rows = [candidate["features"] for candidate in candidates]
    predictions = {policy: _predict(datasets[policy][0], datasets[policy][1], rows) for policy in POLICIES}
    ranks = {policy: _ranks(candidates, predictions[policy]) for policy in POLICIES}
    items = []
    for index, candidate in enumerate(candidates):
        old, new = predictions["old_median"][index], predictions["repetition_balance"][index]
        items.append({
            "id": candidate["id"],
            "group": candidate["group"],
            "old_prediction": _round(old),
            "new_prediction": _round(new),
            "delta": _round(new - old),
            "old_rank": ranks["old_median"][index],
            "new_rank": ranks["repetition_balance"][index],
        })
    top_changed = sorted({
        item["group"] for item in items
        if (item["old_rank"] == 1) != (item["new_rank"] == 1)
    })
    return {
        "status": "compared",
        "training": "real configurations only (no synthetic prior); RandomForest as train_artifact, n_jobs=1",
        "candidates": len(items),
        "groups": len({item["group"] for item in items}),
        "rank_changes": sum(item["old_rank"] != item["new_rank"] for item in items),
        "top1_changed_groups": top_changed,
        "max_abs_prediction_delta": max((abs(item["delta"]) for item in items), default=0.0),
        "items": items,
    }


def compare(rows: list[dict], candidates_path: Path | None = None) -> dict:
    datasets = {policy: build_dataset(rows, policy) for policy in POLICIES}
    candidates = _load_candidates(candidates_path, datasets["repetition_balance"][0])
    repetition = datasets["repetition_balance"][2]["repetition_balance"]
    return {
        "schema_version": 1,
        "policies": list(POLICIES),
        "relative_span": measurement_balance.RELATIVE_SPAN,
        "retention": {policy: _retention(datasets[policy][2]) for policy in POLICIES},
        "repetition_balance_audit": {
            key: repetition[key]
            for key in ("raw_samples", "effective_speed_groups", "repeat_samples_collapsed", "configurations_with_repeats")
        },
        "targets": _target_changes(datasets["old_median"][1], datasets["repetition_balance"][1]),
        "predictions": _prediction_changes(datasets, candidates),
        "limits": [
            "Offline comparison only; nothing is sent to any server.",
            "Speed groups are observations, not authenticated devices.",
            "Fabricated values spread across groups or configurations are not detected.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--telemetry-file", type=Path, required=True)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    from scripts import train_model

    report = compare(train_model.load_telemetry_file(args.telemetry_file), args.candidates)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
