"""Tests for `recommend()`'s candidate ranking and install-ref building."""

from __future__ import annotations

import questionary
from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name=None,
        vram_total_gb=None,
        vram_free_gb=None,
    )


def test_recommend_builds_choice_values_via_exact_install_ref(monkeypatch, isolated_omm_home):
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

    result = runner.invoke(cli.app, ["recommend"])
    assert result.exit_code == 0, result.stdout

    assert captured_choices[0].value == "ms:org/repo:model.gguf"
    assert captured_options["pointer"] == "❯"
    assert "Enter select" in captured_options["instruction"]


def test_recommend_quiet_suppresses_status_lines(monkeypatch, isolated_omm_home):
    """`--quiet` accepts the flag (issue #80) but used to leave the
    "fetched updated data"/"falling back to static rules" status lines
    printing unconditionally - only the rules-fetch line was ever gated."""
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, True)
    )
    monkeypatch.setattr(cli.rules_mod, "matching_rules", lambda *a, **k: [])

    result = runner.invoke(cli.app, ["recommend", "--quiet"])

    assert result.exit_code == 1
    assert "Fetched updated recommendation data" not in result.output
    assert "No trained model available" not in result.output


def test_recommend_without_quiet_prints_status_lines(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (None, True)
    )
    monkeypatch.setattr(cli.rules_mod, "matching_rules", lambda *a, **k: [])

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 1
    assert "Fetched updated recommendation data" in result.output
    assert "No trained model available" in result.output
