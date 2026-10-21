"""Limit repeated speed observations without pretending to authenticate a device.

One encoded configuration still contributes one training example. Within it,
near-identical speeds form bounded groups and each group contributes once to
the target median. Group membership is anchored on its smallest speed, rather
than on adjacent gaps, so a chain of small changes cannot merge distant values.
No observations or rare configurations are deleted by this step.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

RELATIVE_SPAN = 0.05


def balanced_speed(samples: Sequence[float]) -> tuple[float, int]:
    """Return a repetition-balanced target and the observed speed-group count.

    This protects against repeating one value, including jitter within 5%.
    It does not establish independent devices or prevent fabricated values
    spread across many groups or different feature configurations.
    """
    if not samples or any(not math.isfinite(x) or x <= 0 for x in samples):
        raise ValueError("speed samples must be non-empty, positive and finite")
    groups: list[list[float]] = []
    for value in sorted(samples):
        if not groups or value > groups[-1][0] * (1 + RELATIVE_SPAN):
            groups.append([])
        groups[-1].append(value)
    return statistics.median(statistics.median(group) for group in groups), len(groups)
