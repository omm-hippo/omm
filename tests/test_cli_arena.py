from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import arena, cli, quality


runner = CliRunner()


def _result(text, elapsed=1.0, tokens=10, tps=10.0, memory=1.0):
    return arena.GenerationResult(
        text=text, elapsed=elapsed, tokens=tokens, tokens_per_second=tps, memory_gb=memory
    )


def _patch_engine(monkeypatch, pool=("alpha:latest", "beta:latest", "gamma:latest")):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: "ollama")
    monkeypatch.setattr(cli, "_select_benchmark_engine_for_models", lambda models: "ollama")
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: ("ollama", None))
    monkeypatch.setattr(cli, "_stop_engine_daemon", lambda engine, handle: None)
    monkeypatch.setattr(
        cli,
        "_arena_eligible_models",
        lambda engine: {tag: f"{tag.split(':')[0]}.gguf" for tag in pool},
    )


def _patch_prompts(monkeypatch, *, texts, votes, continues):
    text_iter, vote_iter, continue_iter = iter(texts), iter(votes), iter(continues)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: next(text_iter))
    monkeypatch.setattr(cli, "_ask_single_key", lambda *a, **k: next(vote_iter))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: next(continue_iter))


def pytest_fail_no_generation():
    raise AssertionError("generation must not start on a bad argument")


def test_one_round_writes_a_vote_row(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["why is the sky blue?"], votes=["a"], continues=[False])
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda ref, prompt, **kwargs: _result(f"answer from {ref}"),
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest"])
    assert result.exit_code == 0, result.output
    rows = [
        json.loads(line)
        for line in arena.votes_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["model_a"] == "alpha.gguf"
    assert rows[0]["model_b"] == "beta.gguf"
    assert rows[0]["winner"] == "a"
    assert rows[0]["pinned"] is False


def test_blind_phase_leaks_no_identity_before_the_vote(monkeypatch, isolated_omm_home):
    """The single most important assertion in this feature."""
    _patch_engine(monkeypatch)
    seen_before_vote = {}
    captured = []

    def _live_output():
        return "\n".join(captured)

    def _vote(*args, **kwargs):
        seen_before_vote["output"] = _live_output()
        return "a"

    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli, "_ask_single_key", _vote)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda ref, prompt, **kwargs: _result(
            "a neutral answer", elapsed=7.25, tokens=99, tps=13.6
        ),
    )

    real_print = cli.console.print
    monkeypatch.setattr(
        cli.console,
        "print",
        lambda *a, **k: captured.append(" ".join(str(x) for x in a)) or real_print(*a, **k),
    )

    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest"])
    assert result.exit_code == 0, result.output
    before = seen_before_vote["output"]
    for leak in ("alpha", "beta", "Ollama", "ollama", "7.2", "99", "13.6", "tok/s"):
        assert leak not in before, f"blind phase leaked {leak!r}:\n{before}"
    assert "Response 1" in before and "Response 2" in before
    # ...and the reveal, after the vote, does name them.
    assert "alpha:latest" in result.output and "beta:latest" in result.output


def test_keep_reuses_the_same_pair_every_round(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(
        monkeypatch, texts=["p1", "p2"], votes=["a", "b"], continues=[True, False]
    )
    monkeypatch.setattr(
        cli.arena, "generate_side", lambda ref, prompt, **kwargs: _result("x")
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest", "--keep"])
    assert result.exit_code == 0, result.output
    rows = [
        json.loads(line)
        for line in arena.votes_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert {(r["model_a"], r["model_b"]) for r in rows} == {("alpha.gguf", "beta.gguf")}
    assert all(r["pinned"] is True for r in rows)


def test_one_positional_model_errors_before_any_generation(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(
        cli.arena, "generate_side", lambda *a, **k: pytest_fail_no_generation()
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest"])
    assert result.exit_code == 1
    assert "two models" in result.output


def test_unmanaged_model_argument_names_the_import_fix(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "stranger:latest"])
    assert result.exit_code == 1
    assert "stranger:latest" in result.output
    assert "omm import" in result.output


def test_fewer_than_two_eligible_models_errors(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch, pool=("alpha:latest",))
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 1
    assert "at least 2" in result.output


def test_escape_at_the_text_prompt_ends_the_session_without_a_vote(
    monkeypatch, isolated_omm_home
):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: None)
    monkeypatch.setattr(
        cli.arena, "generate_side", lambda *a, **k: pytest_fail_no_generation()
    )
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert not arena.votes_path().exists()


def test_escape_at_the_vote_prompt_ends_the_session_without_a_row(
    monkeypatch, isolated_omm_home
):
    """_ask_single_key cancels by raising KeyboardInterrupt (cli.py:3639),
    not by returning None like _ask_text does."""
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))

    def _cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_ask_single_key", _cancel)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert not arena.votes_path().exists()


def test_bare_enter_at_the_vote_prompt_reasks_instead_of_ending(
    monkeypatch, isolated_omm_home
):
    """_ask_single_key returns default_value on Enter. Ending a battle
    session on a stray Enter would throw away a round the user already
    waited through."""
    _patch_engine(monkeypatch)
    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    answers = iter([None, "b"])
    monkeypatch.setattr(cli, "_ask_single_key", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    rows = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["winner"] == "b"


def test_a_failed_round_writes_no_row_and_keeps_the_session_alive(
    monkeypatch, isolated_omm_home
):
    _patch_engine(monkeypatch)
    calls = {"n": 0}

    def _generate(ref, prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise quality.QualityEvaluationError(
                "ran out of memory", failure_reason="out_of_memory"
            )
        return _result("fine")

    monkeypatch.setattr(cli.arena, "generate_side", _generate)
    _patch_prompts(monkeypatch, texts=["p1", "p2"], votes=["b"], continues=[True, False])
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    rows = arena.votes_path().read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1, "only the second, successful round is recorded"
    assert "memory" in result.output.lower()


def test_arena_never_uploads(monkeypatch, isolated_omm_home):
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["p"], votes=["a"], continues=[False])
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("arena must not upload")),
    )
    assert runner.invoke(cli.app, ["arena"]).exit_code == 0
