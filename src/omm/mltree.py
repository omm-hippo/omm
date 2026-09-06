"""Dependency-free predictor for the recommendation model.

The model ships as plain JSON (not pickle/joblib) specifically so omm can
download it from the internet and run it without trusting arbitrary code
execution from a deserialized object, and without requiring scikit-learn
as a runtime dependency. Trees are trained with scikit-learn in CI
(scripts/train_model.py) and exported to this JSON shape there.

Each tree node is either:
  {"leaf": true, "value": <float>}
  {"feature": <int index into FEATURE_ORDER>, "threshold": <float>,
   "left": <node>, "right": <node>}   # left = feature <= threshold

An ensemble is a list of trees; the prediction is their plain average
(a single-tree "ensemble" is just a list of length 1).
"""

from __future__ import annotations

import math
from typing import Any


# Shared producer/runtime limits for the plain-JSON tree format. Keeping these
# beside the evaluator prevents the training gate and CLI loader from drifting.
MAX_TREE_DEPTH = 256
MAX_TREES = 512
MAX_TOTAL_TREE_NODES = 250_000
MAX_CANDIDATES = 4_096


def predict_tree(node: dict[str, Any], features: list[float]) -> float:
    while not node.get("leaf"):
        value = features[node["feature"]]
        node = node["left"] if value <= node["threshold"] else node["right"]
    return node["value"]


def predict_ensemble(trees: list[dict[str, Any]], features: list[float]) -> float:
    if not trees:
        raise ValueError("empty ensemble")
    return sum(predict_tree(tree, features) for tree in trees) / len(trees)


def predict_ensemble_range(
    trees: list[dict[str, Any]], features: list[float]
) -> tuple[float, float, float]:
    """Return mean plus the 10th/90th tree prediction spread.

    This is an estimator-disagreement range, not a statistical confidence
    interval. It is still more honest than presenting a sparse bootstrap
    prediction as one exact decimal value.
    """
    if not trees:
        raise ValueError("empty ensemble")
    values = sorted(max(0.0, float(predict_tree(tree, features))) for tree in trees)
    mean = sum(values) / len(values)
    low = values[int((len(values) - 1) * 0.1)]
    high = values[math.ceil((len(values) - 1) * 0.9)]
    return mean, low, high
