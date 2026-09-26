from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from omm import arena, config


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
