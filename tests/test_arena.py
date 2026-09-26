from __future__ import annotations

import json
import random
from dataclasses import dataclass

import pytest

from omm import arena, config, quality


@dataclass
class _FakeResult:
    elapsed: float
    tokens: int
    memory_gb: float | None


def _row(**overrides):
    base = dict(
        prompt="Write a haiku about disks.",
        model_a="qwen3-4b-Q4_K_M.gguf",
        model_b="llama3-8b-Q4_K_M.gguf",
        engine="ollama",
        result_a=_FakeResult(elapsed=1.5, tokens=40, memory_gb=3.1),
        result_b=_FakeResult(elapsed=2.5, tokens=60, memory_gb=None),
        winner="a",
        kept=False,
    )
    base.update(overrides)
    return arena.build_vote_row(**base)


def test_votes_path_follows_omm_home(isolated_omm_home):
    assert arena.votes_path() == config.OMM_HOME / "arena" / "votes.jsonl"


def test_build_vote_row_has_exactly_the_spec_fields():
    row = _row()
    assert set(row) == {
        "battle_id", "timestamp", "prompt",
        "model_a", "model_b", "engine_a", "engine_b",
        "elapsed_a", "elapsed_b", "tokens_a", "tokens_b",
        "memory_gb_a", "memory_gb_b", "watt_a", "watt_b",
        "winner", "pinned",
    }
    assert row["engine_a"] == "ollama"
    assert row["engine_b"] == "ollama"
    assert row["watt_a"] is None and row["watt_b"] is None
    assert row["memory_gb_a"] == 3.1
    assert row["memory_gb_b"] is None
    assert row["timestamp"].endswith("+00:00")
    assert len(row["battle_id"]) == 36


def test_build_vote_row_rejects_an_unknown_winner():
    with pytest.raises(ValueError, match="winner"):
        _row(winner="tie")


def test_append_vote_writes_one_json_object_per_line(isolated_omm_home):
    assert arena.append_vote(_row(winner="a")) is True
    assert arena.append_vote(_row(winner="both_bad")) is True
    lines = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["winner"] for line in lines] == ["a", "both_bad"]


def test_append_vote_returns_false_instead_of_raising_when_unwritable(
    isolated_omm_home, monkeypatch
):
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(arena.Path, "mkdir", _boom)
    assert arena.append_vote(_row()) is False


POOL = ["m1", "m2", "m3", "m4"]


def test_seed_pair_is_used_for_round_one_only():
    pairing = arena.Pairing(POOL, seed_pair=("m1", "m2"), rng=random.Random(0))
    assert pairing.next_pair() == ("m1", "m2")
    later = [pairing.next_pair() for _ in range(20)]
    assert any(pair != ("m1", "m2") for pair in later)


def test_keep_holds_the_seed_pair_for_every_round():
    pairing = arena.Pairing(POOL, seed_pair=("m1", "m2"), keep=True, rng=random.Random(0))
    assert [pairing.next_pair() for _ in range(5)] == [("m1", "m2")] * 5


def test_keep_holds_a_randomly_drawn_pair_too():
    pairing = arena.Pairing(POOL, keep=True, rng=random.Random(7))
    first = pairing.next_pair()
    assert [pairing.next_pair() for _ in range(4)] == [first] * 4


def test_random_draw_never_pairs_a_model_with_itself():
    pairing = arena.Pairing(POOL, rng=random.Random(3))
    for _ in range(200):
        left, right = pairing.next_pair()
        assert left != right
        assert left in POOL and right in POOL


def test_random_draw_does_not_favour_either_slot():
    """A draw that always put the lower-indexed model in slot A would leak
    identity across rounds to an attentive user and bias sub-project C's
    Bradley-Terry fit, which reads `winner` against a/b positions."""
    pairing = arena.Pairing(["m1", "m2"], rng=random.Random(11))
    seen = {pairing.next_pair() for _ in range(200)}
    assert seen == {("m1", "m2"), ("m2", "m1")}


def test_validate_seed_pair_rejects_one_model():
    with pytest.raises(arena.ArenaError, match="two models"):
        arena.validate_seed_pair(["m1"], POOL)


def test_validate_seed_pair_rejects_the_same_model_twice():
    with pytest.raises(arena.ArenaError, match="two different"):
        arena.validate_seed_pair(["m1", "m1"], POOL)


def test_validate_seed_pair_rejects_a_model_outside_the_pool():
    with pytest.raises(arena.ArenaError, match="nope"):
        arena.validate_seed_pair(["m1", "nope"], POOL)


def test_validate_seed_pair_returns_the_pair():
    assert arena.validate_seed_pair(["m2", "m3"], POOL) == ("m2", "m3")


def test_generate_side_times_the_call_and_unloads_afterwards(monkeypatch):
    calls = []
    clock = iter([100.0, 103.5])

    monkeypatch.setattr(arena.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        arena.quality,
        "_generate",
        lambda tag, prompt, generation, **kwargs: {
            "response": "  an answer  ",
            "eval_count": 70,
            "eval_duration": 3_500_000_000,
        },
    )
    monkeypatch.setattr(arena.quality, "loaded_model_memory_gb", lambda tag: 2.5)
    monkeypatch.setattr(
        arena.quality, "ensure_model_unloaded", lambda tag: calls.append(tag) or True
    )

    result = arena.generate_side("m:latest", "hi", engine="ollama", lmstudio_port=None)
    assert result.text == "an answer"
    assert result.elapsed == pytest.approx(3.5)
    assert result.tokens == 70
    assert result.tokens_per_second == pytest.approx(20.0)
    assert result.memory_gb == 2.5
    assert calls == ["m:latest"]


def test_generate_side_unloads_even_when_generation_fails(monkeypatch):
    calls = []

    def _boom(*args, **kwargs):
        raise quality.QualityEvaluationError("oom", failure_reason="out_of_memory")

    monkeypatch.setattr(arena.quality, "_generate", _boom)
    monkeypatch.setattr(
        arena.quality, "ensure_model_unloaded", lambda tag: calls.append(tag) or True
    )
    with pytest.raises(quality.QualityEvaluationError):
        arena.generate_side("m:latest", "hi", engine="ollama", lmstudio_port=None)
    assert calls == ["m:latest"]


def test_generate_side_on_lmstudio_reports_null_memory_and_unloads_there(monkeypatch):
    unloaded = []
    monkeypatch.setattr(
        arena.quality,
        "_generate_lmstudio",
        lambda key, prompt, generation, num_predict, port: {
            "response": "ok", "eval_count": 10, "eval_duration": 1_000_000_000
        },
    )
    monkeypatch.setattr(
        arena.quality,
        "loaded_model_memory_gb",
        lambda tag: pytest.fail("LM Studio must not be asked for Ollama's /api/ps"),
    )
    monkeypatch.setattr(
        arena.linker, "unload_lmstudio_model", lambda key: unloaded.append(key) or True
    )
    result = arena.generate_side("key", "hi", engine="lmstudio", lmstudio_port=1234)
    assert result.memory_gb is None
    assert result.tokens == 10
    assert unloaded == ["key"]
