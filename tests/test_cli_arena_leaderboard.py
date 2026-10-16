"""`omm arena --leaderboard` (sub-project D, #410)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from omm import arena_board, arena_upload, cli, session_cache

runner = CliRunner()


def _model(key, *, tier=1, display, repo=None, provisional=False, rating=None,
           component=0, warning=False, strength=0.42):
    efficiency = None
    if rating is not None:
        efficiency = {"rating": rating, "component": component,
                      "raw_median_tok_s_per_gb": 9.0, "sample": 10}
    return {
        "key": key, "display_filename": display, "repo_id": repo, "provider": "huggingface",
        "quant_bits": 4.0, "battles": 57, "effective_battles": 50.0,
        "provisional": provisional,
        "quality": {"strength": strength, "ci_low": strength - 0.31, "ci_high": strength + 0.31,
                    "tier": None if provisional else tier},
        "both_bad_rate": 0.4 if warning else 0.0, "quality_warning": warning,
        "efficiency": efficiency,
        "efficiency_comparable": efficiency is not None and component == 0,
    }


MODELS = [
    _model("sha256:" + "a" * 64, display="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
           repo="org/qwen-gguf", rating=0.336),
    _model("sha256:" + "b" * 64, display="Llama-3.1-8B-Instruct-Q4_K_M.gguf",
           repo="org/llama-gguf", rating=0.1, warning=True),
    _model("sha256:" + "c" * 64, tier=2, display="gemma-2-9b-Q5_K_M.gguf",
           repo="org/gemma-gguf", rating=1.2, component=1, strength=0.11),
    _model("filename:local-only-q4_k_m.gguf", tier=2, display="local-only-Q4_K_M.gguf",
           strength=0.06),
    _model("sha256:" + "d" * 64, display="thin-Q4_K_M.gguf", repo="org/thin",
           provisional=True),
]


def _scope(models, *, ranked=True, votes=340, clients=12):
    return {
        "corpus": {"rows_used": votes, "effective_votes": float(votes), "client_count": clients},
        "models": models if ranked else [],
        "tiers": [], "efficiency_components": [],
        "provisional_models": [m["key"] for m in models if m["provisional"]] if ranked else [],
        "pairs": [], "ranked": ranked,
        "reasons": [] if ranked else ["only 37.0 effective votes, need 200"],
        "updated_at": "2026-10-16T04:00:00+00:00" if ranked else None,
    }


def _artifact(*, overall_ranked=True, group_ranked=True):
    return {
        "schema_version": 2, "generated_at": "2026-10-16T04:00:00+00:00",
        "status": "ranked" if (overall_ranked or group_ranked) else "collecting",
        **_scope(MODELS, ranked=overall_ranked, votes=900 if overall_ranked else 37,
                 clients=40 if overall_ranked else 4),
        "groups": [
            {"key": "unified:16", "memory_kind": "unified", "memory_bucket": "16",
             **_scope(MODELS, ranked=group_ranked, votes=340 if group_ranked else 40,
                      clients=12 if group_ranked else 3)},
        ],
    }


REGISTRY = {"renamed-qwen.gguf": {"sha256": "a" * 64}, "local-only-Q4_K_M.gguf": {}}


@pytest.fixture
def board(monkeypatch, isolated_omm_home):
    state = {"artifact": _artifact(), "group": ("unified", "16"), "registry": dict(REGISTRY)}

    def fetch(public_key):
        value = state["artifact"]
        if isinstance(value, Exception):
            raise value
        return None if value is None else arena_board.Board(value, cached=state.get("cached", False))

    monkeypatch.setattr(arena_board, "fetch_board", fetch)
    monkeypatch.setattr(arena_upload, "current_memory_group", lambda: state["group"])
    monkeypatch.setattr(cli.registry, "load_registry", lambda: state["registry"])
    monkeypatch.setattr(session_cache.os, "ttyname", lambda fd: "/dev/faketty9", raising=False)
    return state


def _run(*args):
    return runner.invoke(cli.app, ["arena", "--leaderboard", *args])


def test_leaderboard_cannot_be_combined_with_models(board):
    result = runner.invoke(cli.app, ["arena", "--leaderboard", "a.gguf", "b.gguf"])
    assert result.exit_code == 1
    assert "--leaderboard" in result.output


def test_leaderboard_cannot_be_combined_with_keep(board):
    result = runner.invoke(cli.app, ["arena", "--leaderboard", "--keep"])
    assert result.exit_code == 1
    assert "--keep" in result.output


def test_leaderboard_never_starts_an_engine(board, monkeypatch):
    def touched(*args, **kwargs):
        raise AssertionError("engine touched")

    monkeypatch.setattr(cli, "_select_benchmark_engine", touched)
    monkeypatch.setattr(cli, "_ensure_engine_running", touched)
    assert _run().exit_code == 0


def test_ranked_group_output(board):
    result = _run()
    out = result.output
    assert result.exit_code == 0, out
    assert "Arena leaderboard · Mac · unified memory 16GB (this machine)" in out
    assert "340 votes from 12 machines" in out
    assert "updated 2026-10-16 04:00 UTC" in out
    assert "signature verified" in out
    assert "TIER 1" in out and "TIER 2" in out
    assert "[1]" in out and "[4]" in out and "[5]" not in out
    assert "Qwen2.5-7B-Instruct" in out and "Q4_K_M" in out
    assert "0.42±0.31" in out
    assert "1.4×" in out
    assert "installed" in out
    assert "often fails" in out
    assert "1 more model needs more votes" in out
    assert "https://omm.run/en/leaderboard" in out
    assert "not comparable" in out  # gemma's rating lives in component 1


def test_the_next_block_only_offers_commands_that_apply(board):
    out = _run().output
    assert "omm install 2" in out  # Llama: first numbered, not installed, sourced
    assert "omm arena 1 4" in out  # the two installed rows
    assert "omm info 1" in out


def test_no_installed_pair_drops_the_arena_hint(board):
    board["registry"] = {}
    out = _run().output
    assert "omm arena 1" not in out and "battle two installed" not in out
    assert "omm install 1" in out


def test_unranked_group_falls_back_with_one_line(board):
    board["artifact"] = _artifact(group_ranked=False)
    out = _run().output
    assert (
        "Your group (Mac · unified memory 16GB) needs more votes — showing all machines."
        in out
    )
    assert "Arena leaderboard · All machines" in out
    assert "(this machine)" not in out
    assert "900 votes from 40 machines" in out


def test_collecting_shows_real_counts_and_no_numbers(board):
    board["artifact"] = _artifact(overall_ranked=False, group_ranked=False)
    session_cache.record_results(["previous.gguf"])
    result = _run()
    out = result.output
    assert result.exit_code == 0
    assert "collecting votes" in out
    assert "37 votes from 4 machines" in out
    assert "Your group (Mac · unified memory 16GB): 40 votes from 3 machines" in out
    assert "omm arena" in out
    assert "[1]" not in out and "TIER" not in out
    assert session_cache.load_last_results() == ["previous.gguf"]


def test_unpublished_exits_zero(board):
    board["artifact"] = None
    result = _run()
    assert result.exit_code == 0
    assert "hasn't been published yet" in result.output
    assert "omm arena" in result.output


@pytest.mark.parametrize(
    "error,text",
    [
        (arena_board.BoardVerificationError("bad"), "signature"),
        (arena_board.BoardUnavailable("offline"), "Couldn't reach"),
        (arena_board.BoardTooNew("v3"), "omm update"),
    ],
)
def test_a_board_error_shows_no_ranking(board, error, text):
    board["artifact"] = error
    result = _run()
    assert result.exit_code == 1
    assert text in result.output
    assert "TIER" not in result.output and "[1]" not in result.output


@pytest.mark.parametrize(
    "state",
    ["ranked", "fallback", "collecting", "unpublished", "no_group"],
)
def test_json_shape_is_identical_across_states(board, state):
    if state == "fallback":
        board["artifact"] = _artifact(group_ranked=False)
    elif state == "collecting":
        board["artifact"] = _artifact(overall_ranked=False, group_ranked=False)
    elif state == "unpublished":
        board["artifact"] = None
    elif state == "no_group":
        board["group"] = None
    result = runner.invoke(cli.app, ["arena", "--leaderboard", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    data = data.get("data", data)
    assert set(data) == {
        "status", "machine_group", "scope", "generated_at", "corpus", "models",
        "provisional_count",
    }
    for model in data["models"]:
        assert {"number", "ref", "installed", "key", "quality"} <= set(model)


def test_json_numbers_and_refs(board):
    data = json.loads(runner.invoke(cli.app, ["arena", "--leaderboard", "--json"]).output)
    data = data.get("data", data)
    assert data["status"] == "ranked"
    assert data["scope"] == {"key": "unified:16", "label": "Mac · unified memory 16GB",
                             "ranked": True, "fell_back": False}
    assert data["machine_group"]["key"] == "unified:16"
    assert [m["number"] for m in data["models"]] == [1, 2, 3, 4]
    assert data["models"][0]["ref"] == "renamed-qwen.gguf"
    assert data["models"][0]["installed"] is True
    assert data["provisional_count"] == 1


def test_numbered_refs_feed_install_and_arena(board):
    assert _run().exit_code == 0
    assert cli._resolve_ref("2") == "org/llama-gguf:Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    pool = {"qwen:latest": "renamed-qwen.gguf", "local:latest": "local-only-Q4_K_M.gguf"}
    assert cli._arena_normalize_refs(["1", "4"], pool) == ["qwen:latest", "local:latest"]


def test_an_unsourced_number_errors_plainly(board):
    board["registry"] = {}
    assert _run().exit_code == 0
    result = runner.invoke(cli.app, ["install", "4"])
    assert result.exit_code == 1
    assert "can't be installed from the leaderboard" in result.output
    assert "local-only-Q4_K_M.gguf" in result.output


def test_a_battle_with_json_is_still_unsupported(board, monkeypatch):
    """Only the leaderboard has a JSON form; a blind battle is interactive."""
    def touched(*args, **kwargs):
        raise AssertionError("engine touched")

    monkeypatch.setattr(cli, "_select_benchmark_engine", touched)
    result = runner.invoke(cli.app, ["arena", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["error"]["code"] == "unsupported_json"


def test_a_cached_board_says_so(board):
    board["cached"] = True
    out = _run().output
    assert "cached copy" in out
    assert "signature verified" in out


def test_a_live_board_does_not_say_cached(board):
    assert "cached copy" not in _run().output


def test_the_signature_error_mentions_a_possible_mid_update(board):
    board["artifact"] = arena_board.BoardVerificationError("bad")
    assert "mid-update" in _run().output
