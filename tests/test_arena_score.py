"""Sub-project C scoring math (scripts/arena_score.py)."""

from __future__ import annotations

import math

import pytest

from scripts import arena_score


def _row(**overrides) -> dict:
    """A minimal valid vote row, shaped exactly like arena_upload's wire row."""
    row = {
        "schema_version": 1,
        "battle_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "abcdef12",
        "recorded_at": "2026-09-26T10:00:00+00:00",
        "engine": "ollama",
        "winner": "a",
        "model_filename_a": "alpha-q4_k_m.gguf",
        "model_filename_b": "beta-q4_k_m.gguf",
        "elapsed_a": 4.0,
        "elapsed_b": 5.0,
        "tokens_a": 100,
        "tokens_b": 120,
        "tokens_per_second_a": 40.0,
        "tokens_per_second_b": 20.0,
        "memory_gb_a": 4.0,
        "memory_gb_b": 4.0,
    }
    row.update(overrides)
    return row


def test_digest_wins_over_filename_for_identity():
    digest = "a" * 64
    assert arena_score.model_key(_row(model_digest_a=digest), "a") == f"sha256:{digest}"


def test_filename_is_the_fallback_and_is_normalized():
    key = arena_score.model_key(_row(model_filename_a="Alpha-Q4_K_M.GGUF"), "a")
    assert key == "filename:alpha-q4_k_m.gguf"


def test_a_side_with_no_usable_identity_drops_the_row():
    battles, dropped = arena_score.validate_rows([_row(model_filename_a="  ")])
    assert battles == []
    assert dropped["identity"] == 1


def test_a_self_battle_is_dropped():
    digest = "b" * 64
    battles, dropped = arena_score.validate_rows(
        [_row(model_digest_a=digest, model_digest_b=digest)]
    )
    assert battles == []
    assert dropped["self_battle"] == 1


def test_an_unknown_winner_is_dropped():
    battles, dropped = arena_score.validate_rows([_row(winner="tie")])
    assert battles == []
    assert dropped["winner"] == 1


def test_a_duplicate_battle_id_is_counted_once():
    # B's upload queue can re-send a row after a partial flush, and the RTDB
    # key is a PoW digest rather than the battle id, so duplicates are storable.
    battles, dropped = arena_score.validate_rows([_row(), _row()])
    assert len(battles) == 1
    assert dropped["duplicate_battle_id"] == 1


def test_rows_are_sorted_so_input_order_cannot_change_the_result():
    early = _row(
        battle_id="1" * 8 + "-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T09:00:00+00:00",
    )
    late = _row(
        battle_id="2" * 8 + "-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T11:00:00+00:00",
    )
    forward, _ = arena_score.validate_rows([early, late])
    backward, _ = arena_score.validate_rows([late, early])
    assert [b.battle_id for b in forward] == [b.battle_id for b in backward]


def test_a_zero_speed_row_stays_a_vote_but_leaves_the_efficiency_axis():
    # ln(0) is -inf and would poison every rating in the component. The vote
    # itself is still valid.
    battles, dropped = arena_score.validate_rows([_row(tokens_per_second_a=0.0)])
    assert len(battles) == 1
    assert battles[0].efficiency_a is None
    assert battles[0].efficiency_b is None
    assert dropped == {}


def test_a_memory_reading_below_the_floor_leaves_the_efficiency_axis():
    battles, _ = arena_score.validate_rows([_row(memory_gb_a=0.0)])
    assert battles[0].efficiency_a is None


def test_efficiency_is_tokens_per_second_over_gibibytes():
    battles, _ = arena_score.validate_rows([_row()])
    assert battles[0].efficiency_a == 10.0  # 40 tok/s over 4 GiB
    assert battles[0].efficiency_b == 5.0


def test_one_client_repeating_one_pair_is_capped_at_ten_effective_votes():
    rows = [
        _row(battle_id=f"{index:08d}-1111-1111-1111-111111111111")
        for index in range(1000)
    ]
    battles, _ = arena_score.validate_rows(rows)
    weights = arena_score.battle_weights(battles)
    assert len(weights) == 1000
    assert sum(weights) == 10.0


def test_a_client_under_the_cap_keeps_full_weight():
    rows = [
        _row(battle_id=f"{index:08d}-1111-1111-1111-111111111111")
        for index in range(3)
    ]
    battles, _ = arena_score.validate_rows(rows)
    assert arena_score.battle_weights(battles) == [1.0, 1.0, 1.0]


def _battle(key_a: str, key_b: str, winner: str, index: int, client: str = "abcdef12"):
    return arena_score.Battle(
        battle_id=f"{index:08d}-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T10:00:00+00:00",
        client_id=client,
        key_a=key_a,
        key_b=key_b,
        winner=winner,
        efficiency_a=None,
        efficiency_b=None,
    )


def _fit(battles):
    return arena_score.fit_quality(battles, arena_score.battle_weights(battles))


def test_symmetric_results_give_equal_strengths():
    battles = [_battle("A", "B", "a", 0), _battle("A", "B", "b", 1)]
    strengths = _fit(battles)
    assert strengths["A"] == pytest.approx(strengths["B"])


def test_a_dominance_chain_comes_out_in_order():
    battles = []
    index = 0
    for _ in range(20):
        battles.append(_battle("A", "B", "a", index))
        index += 1
        battles.append(_battle("B", "C", "a", index))
        index += 1
    strengths = _fit(battles)
    assert strengths["A"] > strengths["B"] > strengths["C"]


def test_the_prior_shrinks_a_thin_undefeated_record():
    thin = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(3)]
    thick = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(30)]
    assert _fit(thin)["A"] < _fit(thick)["A"]


def test_both_bad_rows_do_not_feed_the_fit():
    with_both_bad = [
        _battle("A", "B", "a", 0),
        _battle("A", "B", "both_bad", 1),
        _battle("A", "B", "both_bad", 2),
    ]
    without = [_battle("A", "B", "a", 0)]
    assert _fit(with_both_bad)["A"] == pytest.approx(_fit(without)["A"])


def test_a_model_with_only_both_bad_battles_still_gets_a_finite_strength():
    # Zero wins and zero losses: the fit is prior-only. It must not be NaN.
    battles = [_battle("A", "B", "both_bad", index) for index in range(5)]
    strengths = _fit(battles)
    assert math.isfinite(strengths["A"])
    assert math.isfinite(strengths["B"])
    assert strengths["A"] == pytest.approx(strengths["B"])


def test_an_undefeated_model_does_not_diverge():
    battles = [_battle("A", "B", "a", index, client=f"{index:08x}") for index in range(50)]
    strengths = _fit(battles)
    assert math.isfinite(strengths["A"])
    assert strengths["A"] > strengths["B"]


def test_weights_are_honored_by_the_fit():
    # One client hammering one pair must not beat many clients voting the other
    # way, even with far more rows.
    spam = [_battle("A", "B", "a", index, client="deadbeef") for index in range(1000)]
    honest = [
        _battle("A", "B", "b", 2000 + index, client=f"{index:08x}") for index in range(30)
    ]
    strengths = _fit(spam + honest)
    assert strengths["B"] > strengths["A"]


def test_effective_battles_counts_weight_on_both_sides():
    battles = [_battle("A", "B", "a", 0), _battle("A", "C", "a", 1)]
    counts = arena_score.effective_battles(battles, arena_score.battle_weights(battles))
    assert counts == {"A": 2.0, "B": 1.0, "C": 1.0}


def test_both_bad_rate_is_weighted_and_per_model():
    battles = [
        _battle("A", "B", "both_bad", 0),
        _battle("A", "B", "a", 1),
        _battle("A", "C", "a", 2),
    ]
    rates = arena_score.both_bad_rates(battles, arena_score.battle_weights(battles))
    assert rates["A"] == pytest.approx(1 / 3)
    assert rates["B"] == pytest.approx(1 / 2)
    assert rates["C"] == pytest.approx(0.0)


def test_a_model_with_only_both_bad_battles_has_rate_one():
    battles = [_battle("A", "B", "both_bad", index) for index in range(4)]
    rates = arena_score.both_bad_rates(battles, arena_score.battle_weights(battles))
    assert rates["A"] == pytest.approx(1.0)
    assert rates["A"] >= arena_score.BOTH_BAD_WARNING_RATE


def test_bootstrap_intervals_bracket_the_point_estimate():
    battles = []
    for index in range(60):
        winner = "a" if index % 4 else "b"
        battles.append(_battle("A", "B", winner, index, client=f"{index:08x}"))
    strengths = _fit(battles)
    intervals = arena_score.bootstrap_intervals(battles, resamples=40)
    low, high = intervals["A"]
    assert low <= strengths["A"] <= high
    assert low < high


def test_bootstrap_intervals_are_reproducible():
    battles = [
        _battle("A", "B", "a" if index % 3 else "b", index, client=f"{index:08x}")
        for index in range(30)
    ]
    first = arena_score.bootstrap_intervals(battles, resamples=25)
    second = arena_score.bootstrap_intervals(battles, resamples=25)
    assert first == second


def test_overlapping_intervals_share_a_tier():
    ranked = [
        ("A", 1.0, 0.5, 1.5),
        ("B", 0.9, 0.4, 1.4),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 1}


def test_a_separated_model_opens_the_next_tier():
    ranked = [
        ("A", 1.0, 0.8, 1.2),
        ("B", 0.1, -0.1, 0.3),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 2}


def test_tiers_anchor_on_the_leader_not_the_previous_model():
    # A~B and B~C overlap pairwise, but C is disjoint from A. Chaining would
    # put all three in one tier and let it drift arbitrarily wide.
    ranked = [
        ("A", 1.0, 0.90, 1.10),
        ("B", 0.95, 0.85, 1.05),
        ("C", 0.80, 0.70, 0.89),
    ]
    assert arena_score.assign_tiers(ranked) == {"A": 1, "B": 1, "C": 2}
