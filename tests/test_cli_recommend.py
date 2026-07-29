"""Tests for `recommend()`'s candidate ranking and install-ref building."""

from __future__ import annotations

import questionary

from omm import cli


def test_recommend_builds_choice_values_via_install_ref(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}
    captured_choices = []
    captured_options = {}

    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fake_select(prompt_text, choices, **kwargs):
        captured_choices.extend(choices)
        captured_options.update(kwargs)
        return _DummySelect()

    class _DummySelect:
        pass

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda select_obj: None)  # cancel path, avoids install()

    try:
        cli.recommend()
    except cli.typer.Exit:
        pass  # typer.Exit(0) on the cancel path is expected - not a real SystemExit
              # when recommend() is called directly instead of via CliRunner

    assert captured_choices[0].value == "ms:org/repo"
    assert captured_options["pointer"] == "❯"
    assert "Enter select" in captured_options["instruction"]
