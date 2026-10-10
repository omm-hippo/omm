import random

import pytest

from scripts.measurement_balance import balanced_speed


def test_repeating_plausible_value_cannot_take_over_target():
    honest = [10.0, 15.0, 20.0]
    before, _ = balanced_speed(honest + [40.0])
    after, count = balanced_speed(honest + [40.0] * 1000)
    assert before == after == 17.5
    assert count == 4


def test_jittered_burst_has_only_one_bounded_group():
    burst = [40 + i / 1000 for i in range(1000)]
    target, count = balanced_speed([10.0, 15.0, 20.0] + burst)
    assert target == 17.5
    assert count == 4


def test_slow_and_fast_rare_observations_are_kept():
    assert balanced_speed([0.1, 1, 10, 100, 1000]) == (10, 5)
    assert balanced_speed([0.1]) == (0.1, 1)


def test_grouping_is_order_invariant_and_does_not_chain():
    values = [10, 10.4, 10.8, 11.2]
    target = balanced_speed(values)
    random.Random(1).shuffle(values)
    assert balanced_speed(values) == target
    assert target[1] == 2


@pytest.mark.parametrize("values", [[], [0], [-1], [float("nan")], [float("inf")]])
def test_invalid_observations_are_rejected(values):
    with pytest.raises(ValueError):
        balanced_speed(values)
