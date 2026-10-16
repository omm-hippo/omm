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


def _measured(key_a, key_b, e_a, e_b, index, client="abcdef12", winner="a"):
    return arena_score.Battle(
        battle_id=f"{index:08d}-1111-1111-1111-111111111111",
        recorded_at="2026-09-26T10:00:00+00:00",
        client_id=client,
        key_a=key_a,
        key_b=key_b,
        winner=winner,
        efficiency_a=e_a,
        efficiency_b=e_b,
    )


def test_a_uniformly_faster_machine_does_not_change_the_ratings():
    # The whole justification for the paired design. One machine is 4x faster
    # across the board; the ratings must be identical.
    slow = [_measured("A", "B", 7.5, 5.0, 0, client="1111aaaa")]
    fast = [_measured("A", "B", 30.0, 20.0, 1, client="2222bbbb")]
    slow_fit = arena_score.fit_efficiency(slow, arena_score.battle_weights(slow))
    fast_fit = arena_score.fit_efficiency(fast, arena_score.battle_weights(fast))
    assert slow_fit["A"]["rating"] == pytest.approx(fast_fit["A"]["rating"])
    assert slow_fit["B"]["rating"] == pytest.approx(fast_fit["B"]["rating"])


def test_the_raw_median_does_not_cancel_the_machine():
    # Same fixture, proving the reference number is only a reference number.
    slow = [_measured("A", "B", 7.5, 5.0, 0)]
    fast = [_measured("A", "B", 30.0, 20.0, 1)]
    slow_fit = arena_score.fit_efficiency(slow, arena_score.battle_weights(slow))
    fast_fit = arena_score.fit_efficiency(fast, arena_score.battle_weights(fast))
    assert slow_fit["A"]["raw_median_tok_s_per_gb"] == pytest.approx(7.5)
    assert fast_fit["A"]["raw_median_tok_s_per_gb"] == pytest.approx(30.0)


def test_the_more_efficient_model_rates_higher():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(5)]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] > fitted["B"]["rating"]


def test_ratings_are_centered_inside_a_component():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(5)]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] + fitted["B"]["rating"] == pytest.approx(0.0)


def test_a_transitive_chain_recovers_the_ratio_it_never_measured():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("B", "C", 20.0, 10.0, 1),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    recovered = math.exp(fitted["A"]["rating"] - fitted["C"]["rating"])
    assert recovered == pytest.approx(3.0, rel=1e-6)


def test_disconnected_groups_get_different_components():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("C", "D", 30.0, 20.0, 1),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["component"] == fitted["B"]["component"]
    assert fitted["C"]["component"] == fitted["D"]["component"]
    assert fitted["A"]["component"] != fitted["C"]["component"]


def test_the_largest_component_is_component_zero():
    battles = [
        _measured("A", "B", 30.0, 20.0, 0),
        _measured("B", "C", 20.0, 10.0, 1),
        _measured("D", "E", 30.0, 20.0, 2),
    ]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["component"] == 0
    assert fitted["D"]["component"] == 1


def test_a_corpus_with_no_measurements_yields_no_ratings():
    # Every row from LM Studio, which exposes no memory API.
    battles = [_battle("A", "B", "a", index) for index in range(5)]
    assert arena_score.fit_efficiency(battles, arena_score.battle_weights(battles)) == {}


def test_both_bad_rows_still_carry_usable_measurements():
    battles = [_measured("A", "B", 30.0, 20.0, 0, winner="both_bad")]
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["rating"] > fitted["B"]["rating"]


def test_the_sample_count_is_the_number_of_eligible_rows():
    battles = [_measured("A", "B", 30.0, 20.0, index) for index in range(3)]
    battles.append(_battle("A", "B", "a", 99))
    fitted = arena_score.fit_efficiency(battles, arena_score.battle_weights(battles))
    assert fitted["A"]["sample"] == 3


def _corpus(count: int = 60) -> list[dict]:
    """A corpus large enough that both models clear MIN_EFFECTIVE_BATTLES."""
    rows = []
    for index in range(count):
        rows.append(
            _row(
                battle_id=f"{index:08d}-1111-1111-1111-111111111111",
                client_id=f"{index:08x}",
                winner="a" if index % 4 else "b",
            )
        )
    return rows


def test_the_artifact_reports_the_corpus_it_used():
    artifact = arena_score.build_artifact(
        _corpus(), generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert artifact["schema_version"] == arena_score.ARTIFACT_SCHEMA_VERSION
    assert artifact["generated_at"] == "2026-09-27T04:00:00+00:00"
    assert artifact["corpus"]["rows_fetched"] == 60
    assert artifact["corpus"]["rows_used"] == 60
    assert artifact["corpus"]["client_count"] == 60
    assert artifact["corpus"]["effective_votes"] == pytest.approx(60.0)


def test_drop_reasons_are_reported_not_absorbed():
    rows = _corpus(4) + [
        _row(battle_id="ffffffff-1111-1111-1111-111111111111", winner="tie")
    ]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    assert artifact["corpus"]["rows_fetched"] == 5
    assert artifact["corpus"]["rows_used"] == 4
    assert artifact["corpus"]["rows_dropped"] == {"winner": 1}


def test_a_thin_model_is_provisional_and_untiered():
    artifact = arena_score.build_artifact(
        _corpus(4), generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    entry = artifact["models"][0]
    assert entry["provisional"] is True
    assert entry["quality"]["tier"] is None
    assert artifact["tiers"] == []
    assert sorted(artifact["provisional_models"]) == sorted(
        model["key"] for model in artifact["models"]
    )


def test_a_well_supported_model_gets_a_tier():
    artifact = arena_score.build_artifact(
        _corpus(), generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert artifact["provisional_models"] == []
    assert all(model["quality"]["tier"] is not None for model in artifact["models"])
    assert artifact["tiers"][0]["tier"] == 1


def test_metadata_travels_with_the_model():
    rows = _corpus()
    for row in rows:
        row["model_provider_a"] = "huggingface"
        row["model_repo_id_a"] = "Vendor/Alpha-GGUF"
        row["quant_bits_a"] = 4.0
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    alpha = next(
        m for m in artifact["models"] if m["key"] == "filename:alpha-q4_k_m.gguf"
    )
    assert alpha["display_filename"] == "alpha-q4_k_m.gguf"
    assert alpha["repo_id"] == "Vendor/Alpha-GGUF"
    assert alpha["provider"] == "huggingface"
    assert alpha["quant_bits"] == 4.0


def test_the_artifact_is_byte_identical_for_a_shuffled_corpus():
    import json
    import random as stdlib_random

    rows = _corpus()
    shuffled = list(rows)
    stdlib_random.Random(7).shuffle(shuffled)
    first = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    second = arena_score.build_artifact(
        shuffled, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_efficiency_is_null_when_nothing_was_measured():
    rows = _corpus()
    for row in rows:
        del row["memory_gb_a"]
        del row["memory_gb_b"]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=20
    )
    assert all(model["efficiency"] is None for model in artifact["models"])
    assert artifact["efficiency_components"] == []


def test_an_empty_corpus_produces_an_empty_but_valid_artifact():
    artifact = arena_score.build_artifact(
        [], generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    assert artifact["models"] == []
    assert artifact["tiers"] == []
    assert artifact["corpus"]["effective_votes"] == 0.0
    assert artifact["corpus"]["largest_client_share"] == 0.0


def test_the_largest_client_share_is_reported():
    rows = [
        _row(battle_id=f"{index:08d}-1111-1111-1111-111111111111", client_id="deadbeef")
        for index in range(30)
    ]
    rows += [
        _row(
            battle_id=f"{100 + index:08d}-1111-1111-1111-111111111111",
            client_id=f"{index:08x}",
        )
        for index in range(10)
    ]
    artifact = arena_score.build_artifact(
        rows, generated_at="2026-09-27T04:00:00+00:00", resamples=5
    )
    # The spammer's 30 rows are capped to 10 effective votes against 10 honest
    # ones, so its share is half - not 75%.
    assert artifact["corpus"]["largest_client_share"] == pytest.approx(0.5)


# --- sub-project D: one scope, comparable ordering, pair counts -----------


def _two_islands() -> list[dict]:
    """m1/m2 measured against each other, m3/m4 against each other: two
    efficiency components of equal size, so component 0 is {m1, m2} by the
    lowest-key tiebreak. m3 is wildly more efficient than m4, which made the
    old cross-component sort put it first in its tier."""
    rows = []
    index = 0
    for a, b, tps_a, tps_b, a_wins in (
        ("m1.gguf", "m2.gguf", 30.0, 20.0, 20),
        ("m3.gguf", "m4.gguf", 400.0, 10.0, 16),
    ):
        for n in range(40):
            rows.append(
                _row(
                    battle_id=f"{index:08d}-2222-2222-2222-222222222222",
                    client_id=f"{index:08x}",
                    model_filename_a=a,
                    model_filename_b=b,
                    tokens_per_second_a=tps_a,
                    tokens_per_second_b=tps_b,
                    winner="a" if n < a_wins else "b",
                )
            )
            index += 1
    return rows


def test_within_tier_order_never_compares_ratings_across_components():
    scope = arena_score.score_scope(_two_islands(), resamples=20)
    by_key = {model["key"]: model for model in scope["models"]}
    assert by_key["filename:m3.gguf"]["efficiency"]["component"] == 1
    assert by_key["filename:m3.gguf"]["efficiency_comparable"] is False
    assert by_key["filename:m1.gguf"]["efficiency_comparable"] is True
    for tier in {m["quality"]["tier"] for m in scope["models"]} - {None}:
        members = [m for m in scope["models"] if m["quality"]["tier"] == tier]
        flags = [m["efficiency_comparable"] for m in members]
        # Every comparable model comes before every non-comparable one.
        assert flags == sorted(flags, reverse=True)
        comparable = [m for m in members if m["efficiency_comparable"]]
        assert [m["efficiency"]["rating"] for m in comparable] == sorted(
            (m["efficiency"]["rating"] for m in comparable), reverse=True
        )
        rest = [m for m in members if not m["efficiency_comparable"]]
        assert [(-m["quality"]["strength"], m["key"]) for m in rest] == sorted(
            (-m["quality"]["strength"], m["key"]) for m in rest
        )
    first = scope["models"][0]
    assert first["key"] != "filename:m3.gguf"


def test_an_unmeasured_model_is_not_comparable():
    rows = _corpus()
    for row in rows:
        del row["memory_gb_a"]
        del row["memory_gb_b"]
    scope = arena_score.score_scope(rows, resamples=5)
    assert all(model["efficiency_comparable"] is False for model in scope["models"])


def test_score_scope_has_no_artifact_envelope():
    scope = arena_score.score_scope(_corpus(4), resamples=5)
    assert set(scope) == {
        "corpus", "models", "tiers", "efficiency_components", "provisional_models", "pairs"
    }


def test_pair_counts_sum_to_the_scope():
    rows = _two_islands() + [
        _row(battle_id="99999999-2222-2222-2222-222222222222", winner="both_bad")
    ]
    scope = arena_score.score_scope(rows, resamples=5)
    pairs = scope["pairs"]
    assert sum(p["battles"] for p in pairs) == scope["corpus"]["rows_used"]
    assert sum(p["wins_a"] + p["wins_b"] + p["both_bad"] for p in pairs) == pytest.approx(
        scope["corpus"]["effective_votes"]
    )
    assert all(p["a"] < p["b"] for p in pairs)
    assert [(p["a"], p["b"]) for p in pairs] == sorted((p["a"], p["b"]) for p in pairs)
    m1 = next(p for p in pairs if p["a"] == "filename:m1.gguf")
    assert (m1["wins_a"], m1["wins_b"], m1["battles"]) == (20.0, 20.0, 40)


def test_pair_counts_orient_wins_to_the_sorted_key():
    # The row's side "a" is the later key here, so its win counts for b.
    rows = [
        _row(
            battle_id=f"{n:08d}-3333-3333-3333-333333333333",
            client_id=f"{n:08x}",
            model_filename_a="zeta.gguf",
            model_filename_b="alpha.gguf",
            winner="a",
        )
        for n in range(3)
    ]
    (pair,) = arena_score.score_scope(rows, resamples=5)["pairs"]
    assert pair["a"] == "filename:alpha.gguf"
    assert (pair["wins_a"], pair["wins_b"]) == (0.0, 3.0)


def test_pair_counts_respect_the_client_pair_cap():
    rows = [
        _row(battle_id=f"{n:08d}-4444-4444-4444-444444444444", client_id="deadbeef")
        for n in range(30)
    ]
    (pair,) = arena_score.score_scope(rows, resamples=5)["pairs"]
    assert pair["battles"] == 30
    assert pair["wins_a"] == pytest.approx(arena_score.CLIENT_PAIR_VOTE_CAP)


# --- sub-project D: memory groups, artifact v2 ----------------------------


def _grouped_corpus() -> list[dict]:
    rows = []
    for index, (kind, bucket) in enumerate(
        [("unified", "16")] * 30 + [("vram", "8")] * 12 + [(None, None)] * 8
    ):
        row = _row(
            battle_id=f"{index:08d}-5555-5555-5555-555555555555",
            client_id=f"{index:08x}",
            winner="a" if index % 3 else "b",
        )
        if kind is not None:
            row["memory_kind"] = kind
            row["memory_bucket"] = bucket
        rows.append(row)
    return rows


def test_group_key_reads_a_valid_pair_only():
    assert arena_score.group_key(_row(memory_kind="unified", memory_bucket="16")) == "unified:16"
    assert arena_score.group_key(_row()) is None
    assert arena_score.group_key(_row(memory_kind="vram", memory_bucket="18-24")) is None
    assert arena_score.group_key(_row(memory_kind="unified")) is None


def test_a_group_scores_exactly_like_the_scorer_on_its_own_rows():
    rows = _grouped_corpus()
    artifact = arena_score.build_artifact(rows, generated_at="t", resamples=20)
    group = next(g for g in artifact["groups"] if g["key"] == "unified:16")
    alone = arena_score.score_scope(
        [row for row in rows if arena_score.group_key(row) == "unified:16"], resamples=20
    )
    assert {key: group[key] for key in alone} == alone
    assert (group["memory_kind"], group["memory_bucket"]) == ("unified", "16")


def test_rows_without_a_group_count_only_in_overall():
    rows = _grouped_corpus()
    artifact = arena_score.build_artifact(rows, generated_at="t", resamples=5)
    assert artifact["corpus"]["rows_used"] == 50
    assert [g["key"] for g in artifact["groups"]] == ["unified:16", "vram:8"]
    assert sum(g["corpus"]["rows_fetched"] for g in artifact["groups"]) == 42


def test_an_invalid_pair_is_ungrouped_not_dropped():
    rows = _corpus(4)
    rows[0]["memory_kind"] = "vram"
    rows[0]["memory_bucket"] = "18-24"
    artifact = arena_score.build_artifact(rows, generated_at="t", resamples=5)
    assert artifact["corpus"]["rows_used"] == 4
    assert artifact["groups"] == []


def test_the_artifact_is_schema_2_with_no_publication_decision():
    artifact = arena_score.build_artifact(_grouped_corpus(), generated_at="t", resamples=5)
    assert artifact["schema_version"] == 2
    assert "status" not in artifact and "ranked" not in artifact


def test_a_grouped_corpus_is_byte_identical_across_runs():
    import json
    import random as stdlib_random

    rows = _grouped_corpus()
    shuffled = list(rows)
    stdlib_random.Random(3).shuffle(shuffled)
    first = arena_score.build_artifact(rows, generated_at="t", resamples=10)
    second = arena_score.build_artifact(shuffled, generated_at="t", resamples=10)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
