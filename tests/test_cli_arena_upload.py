from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import arena, arena_upload, cli, config


runner = CliRunner()


def _result(text="x"):
    return arena.GenerationResult(
        text=text, elapsed=1.0, tokens=10, tokens_per_second=10.0, memory_gb=1.0
    )


def _vote_row():
    return {
        "battle_id": "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a",
        "timestamp": "2026-09-26T04:05:06.700000+00:00",
        "prompt": "secret",
        "model_a": "alpha.gguf",
        "model_b": "beta.gguf",
        "engine_a": "ollama",
        "engine_b": "ollama",
        "elapsed_a": 1.0,
        "elapsed_b": 2.0,
        "tokens_a": 10,
        "tokens_b": 20,
        "memory_gb_a": None,
        "memory_gb_b": None,
        "tokens_per_second_a": None,
        "tokens_per_second_b": None,
        "watt_a": None,
        "watt_b": None,
        "winner": "a",
        "pinned": False,
    }


def _patch_arena(monkeypatch, pool=("alpha:latest", "beta:latest")):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: "ollama")
    monkeypatch.setattr(cli, "_select_benchmark_engine_for_models", lambda models: "ollama")
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: ("ollama", None))
    monkeypatch.setattr(cli, "_stop_engine_daemon", lambda engine, handle: None)
    monkeypatch.setattr(
        cli,
        "_arena_eligible_models",
        lambda engine: {tag: f"{tag.split(':')[0]}.gguf" for tag in pool},
    )
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result())


def _one_round(monkeypatch, *, vote="a"):
    texts, votes, continues = iter(["p"]), iter([vote]), iter([False])
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: next(texts))
    monkeypatch.setattr(cli, "_ask_single_key", lambda *a, **k: next(votes))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: next(continues))


def _never_ask(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not ask")),
    )


def test_declining_consent_queues_nothing(monkeypatch, isolated_omm_home):
    """Review focus 3: if `n` left rows on disk, a later
    `omm setting upload votes --enable` would ship refused votes."""
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 0
    assert not arena_upload._pending_path().exists()
    # The local record is untouched - declining upload is not declining the vote.
    assert len(arena.votes_path().read_text(encoding="utf-8").splitlines()) == 1


def test_accepting_once_queues_and_leaves_the_policy_ask(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_accepting_always_saves_the_policy(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "always")
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1
    assert config.load_config()["arena_vote_send_policy"] == "always"


def test_an_always_policy_queues_without_asking(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    config.update_config(arena_vote_send_policy="always")
    _never_ask(monkeypatch)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 1


def test_a_never_policy_neither_asks_nor_queues(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    config.update_config(arena_vote_send_policy="never")
    _never_ask(monkeypatch)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert arena_upload.pending_count() == 0


def test_a_session_with_no_recorded_vote_does_not_ask(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: None)
    _never_ask(monkeypatch)
    assert runner.invoke(cli.app, ["arena"]).exit_code == 0


def test_an_enqueue_failure_does_not_break_the_session(monkeypatch, isolated_omm_home):
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    monkeypatch.setattr(
        cli.arena_upload,
        "enqueue",
        lambda rows: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert len(arena.votes_path().read_text(encoding="utf-8").splitlines()) == 1


def test_setting_upload_votes_enable_and_disable(isolated_omm_home):
    enabled = runner.invoke(cli.app, ["setting", "upload", "votes", "--enable"])
    assert enabled.exit_code == 0, enabled.output
    assert config.load_config()["arena_vote_send_policy"] == "always"

    arena_upload.enqueue([_vote_row()])
    assert arena_upload.pending_count() == 1
    disabled = runner.invoke(cli.app, ["setting", "upload", "votes", "--disable"])
    assert disabled.exit_code == 0, disabled.output
    assert config.load_config()["arena_vote_send_policy"] == "never"
    # Turning the channel off discards what was queued under consent.
    assert arena_upload.pending_count() == 0
    assert "1" in disabled.output


def test_setting_upload_votes_ask_restores_the_default(isolated_omm_home):
    config.update_config(arena_vote_send_policy="always")
    result = runner.invoke(cli.app, ["setting", "upload", "votes", "--ask"])
    assert result.exit_code == 0, result.output
    assert config.load_config()["arena_vote_send_policy"] == "ask"


def test_setting_upload_votes_rejects_two_flags(isolated_omm_home):
    both = runner.invoke(cli.app, ["setting", "upload", "votes", "--enable", "--disable"])
    assert both.exit_code == 1
    assert "one of" in both.output


def test_setting_upload_votes_with_no_flags_prints_the_policy(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "upload", "votes"])
    assert result.exit_code == 0, result.output
    assert "ask" in result.output
    assert "Queued" in result.output


def test_the_policy_table_lists_four_channels(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "upload"])
    assert result.exit_code == 0, result.output
    for channel in ("benchmark", "usage", "crash", "votes"):
        assert channel in result.output


def test_root_flush_runs_for_an_ordinary_command(monkeypatch, isolated_omm_home):
    """The queue drains on the next `omm` run, not during a battle."""
    calls = {"n": 0}

    def _flush(*args, **kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(cli.arena_upload, "flush_pending", _flush)
    runner.invoke(cli.app, ["list"])
    assert calls["n"] == 1


def test_the_queued_payload_still_has_no_prompt(monkeypatch, isolated_omm_home):
    """End-to-end through the CLI, not just the builder."""
    _patch_arena(monkeypatch)
    _one_round(monkeypatch)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")
    assert runner.invoke(cli.app, ["arena"]).exit_code == 0
    queued = arena_upload._pending_path().read_text(encoding="utf-8")
    assert "prompt" not in json.loads(queued.splitlines()[0])
    assert "p" == json.loads(
        arena.votes_path().read_text(encoding="utf-8").splitlines()[0]
    )["prompt"]
