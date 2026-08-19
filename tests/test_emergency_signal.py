"""Tests for the emergency-update signal: an optional `emergency` field on
the recommendation-model artifact that omm reacts to (block + strongly
recommend `omm update`) when a critical omm/Firebase/runner incompatibility
is published. See predictor.extract_emergency_signal and
cli._handle_emergency_signal."""

from __future__ import annotations

import subprocess

from omm import cli, predictor


# ---------------------------------------------------------------------------
# predictor.extract_emergency_signal
# ---------------------------------------------------------------------------


def test_extract_emergency_signal_returns_none_when_absent():
    assert predictor.extract_emergency_signal({"candidates": []}) is None
    assert predictor.extract_emergency_signal(None) is None
    assert predictor.extract_emergency_signal("not a dict") is None


def test_extract_emergency_signal_returns_well_formed_signal():
    signal = {"id": "2026-08-19-outage", "message": "Firebase link broken", "fixed_in_version": "0.3.0"}
    assert predictor.extract_emergency_signal({"emergency": signal}) == signal


def test_extract_emergency_signal_rejects_malformed_shapes():
    assert predictor.extract_emergency_signal({"emergency": "not a dict"}) is None
    assert predictor.extract_emergency_signal({"emergency": {}}) is None
    assert predictor.extract_emergency_signal({"emergency": {"message": "  "}}) is None
    assert predictor.extract_emergency_signal(
        {"emergency": {"message": "x", "fixed_in_version": 3}}
    ) is None
    assert predictor.extract_emergency_signal(
        {"emergency": {"message": "x", "id": 3}}
    ) is None


# ---------------------------------------------------------------------------
# cli._version_at_least
# ---------------------------------------------------------------------------


def test_version_at_least_compares_dotted_integers():
    assert cli._version_at_least("0.2.98", "0.2.90") is True
    assert cli._version_at_least("0.2.98", "0.3.0") is False
    assert cli._version_at_least("0.3.0", "0.3.0") is True
    assert cli._version_at_least("1.0", "0.9.9") is True


def test_version_at_least_nags_on_unparseable_input():
    # An emergency signal should never go silent because of an unparseable
    # version string - nag rather than risk a false "already fixed".
    assert cli._version_at_least("dev", "0.3.0") is False
    assert cli._version_at_least("0.2.98", "not-a-version") is False


# ---------------------------------------------------------------------------
# cli._handle_emergency_signal
# ---------------------------------------------------------------------------


def _signal(**overrides):
    base = {"id": "sig-1", "message": "Runner protocol broke, update now."}
    base.update(overrides)
    return base


def test_handle_emergency_signal_noop_when_no_signal(monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    cli._handle_emergency_signal({"candidates": []})  # must not raise/prompt


def test_handle_emergency_signal_skips_when_already_fixed(monkeypatch):
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.3.0")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    cli._handle_emergency_signal({"emergency": _signal(fixed_in_version="0.2.0")})


def test_handle_emergency_signal_updates_and_restarts_on_yes(monkeypatch):
    monkeypatch.setattr(cli, "_emergency_signals_shown", set())
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.98")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_channel_branch", lambda: "beta")

    perform_calls = []
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: perform_calls.append(branch) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    restart_calls = []
    monkeypatch.setattr(cli, "_restart_after_update", lambda: restart_calls.append(1))

    cli._handle_emergency_signal({"emergency": _signal(fixed_in_version="0.3.0")})

    assert perform_calls == ["beta"]
    assert restart_calls == [1]


def test_handle_emergency_signal_blocks_on_no(monkeypatch):
    import typer

    monkeypatch.setattr(cli, "_emergency_signals_shown", set())
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.98")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli, "_perform_update", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )

    try:
        cli._handle_emergency_signal({"emergency": _signal(fixed_in_version="0.3.0")})
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass


def test_handle_emergency_signal_exits_when_update_fails(monkeypatch):
    import typer

    monkeypatch.setattr(cli, "_emergency_signals_shown", set())
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.98")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_channel_branch", lambda: "beta")
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
    )
    restart_calls = []
    monkeypatch.setattr(cli, "_restart_after_update", lambda: restart_calls.append(1))

    try:
        cli._handle_emergency_signal({"emergency": _signal(fixed_in_version="0.3.0")})
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass
    assert restart_calls == []


def test_handle_emergency_signal_non_tty_blocks_without_prompting(monkeypatch):
    import typer

    monkeypatch.setattr(cli, "_emergency_signals_shown", set())
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.98")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    try:
        cli._handle_emergency_signal({"emergency": _signal(fixed_in_version="0.3.0")})
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass


def test_handle_emergency_signal_only_nags_once_per_signal_id(monkeypatch):
    import typer

    monkeypatch.setattr(cli, "_emergency_signals_shown", set())
    monkeypatch.setattr(cli, "_omm_version", lambda: "0.2.98")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    calls = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: calls.append(1) or False)

    signal = {"emergency": _signal(fixed_in_version="0.3.0")}
    try:
        cli._handle_emergency_signal(signal)
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass
    # Second call in the same process (e.g. a later refetch) with the same
    # signal id is a silent no-op rather than blocking/prompting again.
    cli._handle_emergency_signal(signal)

    assert calls == [1]


def test_load_recommendation_with_change_note_triggers_emergency_check(monkeypatch, tmp_path):
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", tmp_path / "recommend-model.json")
    artifact = {"model_version": 4, "feature_order": [], "trees": [{"leaf": True, "value": 1.0}],
                "candidates": [], "emergency": _signal()}
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda url: artifact)

    seen = []
    monkeypatch.setattr(cli, "_handle_emergency_signal", lambda a: seen.append(a))

    cli._load_recommendation_with_change_note({"model_url": "http://example.com/model.json"})

    assert seen == [artifact]
