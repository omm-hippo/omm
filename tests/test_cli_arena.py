from __future__ import annotations

import json
import sys

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
    # Slot order is randomized per round to keep a seeded pair blind, so the
    # row holds the two models without a fixed a/b assignment.
    assert {rows[0]["model_a"], rows[0]["model_b"]} == {"alpha.gguf", "beta.gguf"}
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
    # The same two models every round; slot order is randomized per round so
    # a --keep session stays blind (see test_arena.py's Pairing tests).
    assert {frozenset((r["model_a"], r["model_b"])) for r in rows} == {
        frozenset(("alpha.gguf", "beta.gguf"))
    }
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


def test_a_registry_filename_argument_resolves_to_its_runtime_ref(
    monkeypatch, isolated_omm_home
):
    """Fix for review finding #2: `omm list` prints registry filenames, so
    pasting them is the natural invocation. Rejecting them with a fix hint
    that points back at `omm list` is circular."""
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["p"], votes=["a"], continues=[False])
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    result = runner.invoke(cli.app, ["arena", "alpha.gguf", "beta.gguf"])
    assert result.exit_code == 0, result.output
    row = json.loads(arena.votes_path().read_text(encoding="utf-8").splitlines()[0])
    assert {row["model_a"], row["model_b"]} == {"alpha.gguf", "beta.gguf"}


def test_a_numbered_ref_argument_resolves_to_its_runtime_ref(
    monkeypatch, isolated_omm_home
):
    """`omm list` also prints a numeric index, recorded by session_cache."""
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["p"], votes=["a"], continues=[False])
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    monkeypatch.setattr(
        cli.session_cache, "load_last_results", lambda: ["alpha.gguf", "beta.gguf"]
    )
    result = runner.invoke(cli.app, ["arena", "1", "2"])
    assert result.exit_code == 0, result.output
    row = json.loads(arena.votes_path().read_text(encoding="utf-8").splitlines()[0])
    assert {row["model_a"], row["model_b"]} == {"alpha.gguf", "beta.gguf"}


def test_arena_is_yes_capable_so_the_flag_is_not_reported_as_useless(
    monkeypatch, isolated_omm_home
):
    """Fix for review finding #3: --yes IS forwarded to
    `_ensure_engine_running`, where it skips the "start the daemon?" confirm,
    so the "has no effect" warning is false."""
    assert "arena" in cli._YES_CAPABLE
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=[None], votes=[], continues=[])
    result = runner.invoke(cli.app, ["arena", "--yes"])
    assert result.exit_code == 0, result.output
    assert "has no effect" not in result.output


def test_a_bad_argument_count_errors_before_the_engine_is_started(
    monkeypatch, isolated_omm_home
):
    """Fix for review finding #4: the plan requires the arity error "before
    any engine is started". Starting Ollama and walking the manifest tree
    first, only to reject the argument, wastes the user's time - and on a
    machine with no engine it reports the wrong problem entirely."""
    monkeypatch.setattr(
        cli,
        "_ensure_engine_running",
        lambda *a, **k: pytest_fail_no_generation(),
    )
    monkeypatch.setattr(
        cli, "_select_benchmark_engine", lambda: pytest_fail_no_generation()
    )
    monkeypatch.setattr(
        cli,
        "_select_benchmark_engine_for_models",
        lambda models: pytest_fail_no_generation(),
    )
    one = runner.invoke(cli.app, ["arena", "alpha:latest"])
    assert one.exit_code == 1
    assert "two models" in one.output
    twice = runner.invoke(cli.app, ["arena", "alpha:latest", "alpha:latest"])
    assert twice.exit_code == 1
    assert "two different" in twice.output


def test_no_elapsed_clock_is_rendered_during_the_blind_phase(
    monkeypatch, isolated_omm_home
):
    """Fix for review finding #5: the rich Progress render never goes through
    console.print, so the previous leak test could not see it - and it
    carried a live TimeElapsedColumn, an on-screen pre-vote timing number."""
    _patch_engine(monkeypatch)
    seen = {}

    def _vote(*args, **kwargs):
        # CliRunner's replaced stdout, which DOES include the rich Progress
        # render (console.print does not - that was the old test's blind spot).
        # CliRunner wraps it in a _NamedTextIOWrapper, so read the byte buffer.
        sys.stdout.flush()
        seen["output"] = sys.stdout.buffer.getvalue().decode("utf-8", "replace")
        return "a"

    monkeypatch.setattr(cli, "_ask_text", lambda *a, **k: "hello")
    monkeypatch.setattr(cli, "_ask_single_key", _vote)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.arena,
        "generate_side",
        lambda ref, prompt, **kwargs: _result("neutral", elapsed=7.25, tokens=99, tps=13.6),
    )
    result = runner.invoke(cli.app, ["arena", "alpha:latest", "beta:latest"])
    assert result.exit_code == 0, result.output
    before = seen["output"]
    assert "Response 1" in before, before
    for leak in ("alpha", "beta", "Ollama", "0:00:", "7.2", "99", "13.6"):
        assert leak not in before, f"blind phase leaked {leak!r}:\n{before}"


def test_an_unsaved_round_is_not_counted_as_recorded(monkeypatch, isolated_omm_home):
    """Warning that the vote was not saved and then reporting "1 round(s)
    recorded in .../votes.jsonl" contradicts itself."""
    _patch_engine(monkeypatch)
    _patch_prompts(monkeypatch, texts=["p"], votes=["a"], continues=[False])
    monkeypatch.setattr(cli.arena, "generate_side", lambda *a, **k: _result("x"))
    monkeypatch.setattr(cli.arena, "append_vote", lambda row: False)
    result = runner.invoke(cli.app, ["arena"])
    assert result.exit_code == 0, result.output
    assert "was not saved" in result.output
    assert "round(s) recorded" not in result.output


def test_lmstudio_eligibility_runs_one_lms_listing_for_the_whole_registry(
    monkeypatch, isolated_omm_home
):
    """Fix for review finding #8: `linker.resolve_lmstudio_model` spawns its
    own `lms ls --json` subprocess per call, so a per-registry-entry loop is
    N+1 Node process launches before the user sees a prompt."""
    calls = {"n": 0}
    entries = [
        {"type": "llm", "modelKey": "pub/alpha", "path": "pub/alpha/alpha.gguf"},
        {"type": "llm", "modelKey": "pub/beta", "path": "pub/beta/beta.gguf"},
    ]

    def _list(lms_path, timeout=15):
        calls["n"] += 1
        return entries

    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: "/usr/local/bin/lms")
    monkeypatch.setattr(cli.linker, "_lmstudio_list_models", _list)
    monkeypatch.setattr(
        cli.linker,
        "resolve_lmstudio_model",
        lambda repo_id, filename: pytest_fail_no_generation(),
    )
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "alpha.gguf": {"repo_id": "pub/alpha"},
            "beta.gguf": {"repo_id": "pub/beta"},
        },
    )
    pool = cli._arena_eligible_models("lmstudio")
    assert pool == {"pub/alpha": "alpha.gguf", "pub/beta": "beta.gguf"}
    assert calls["n"] == 1, f"expected one `lms ls --json`, got {calls['n']}"
