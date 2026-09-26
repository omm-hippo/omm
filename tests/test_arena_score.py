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
