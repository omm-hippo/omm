"""Tests for `recommend()`'s candidate ranking and install-ref building."""

from __future__ import annotations

import json

import questionary
import pytest
from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _default_to_uninstalled_candidates(monkeypatch):
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [cli.recommend_status.NOT_INSTALLED] * len(candidates),
    )


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


def test_recommend_json_lists_candidates_without_installing(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fail_install(*a, **k):
        raise AssertionError("install() must not run under --json")

    monkeypatch.setattr(cli, "install", fail_install)
    monkeypatch.setattr(cli, "_select_recommended_model", fail_install)

    result = runner.invoke(cli.app, ["recommend", "--json"])

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["rank"] == 1
    assert row["ref"] == "ms:org/repo:model.gguf"
    assert row["name"] == cli.recommend_ui.humanize_model_name(candidate)
    assert row["predicted_tokens_per_second"] == 42.0
    assert row["installed"] is False
    assert row["managed_by_omm"] is False
    assert row["installed_engines"] == []
    assert row["installation_match"] is None


def test_recommend_yes_installs_top_candidate_without_prompting(monkeypatch, isolated_omm_home):
    candidate = {
        "name": "org/repo",
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "provider": "modelscope",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 42.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)

    def fail_select(*a, **k):
        raise AssertionError("interactive picker must not run under --yes")

    monkeypatch.setattr(cli, "_select_recommended_model", fail_select)

    installed = []
    monkeypatch.setattr(cli, "install", lambda ref: installed.append(ref))

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert installed == ["ms:org/repo:model.gguf"]


def test_recommend_yes_skips_installed_top_candidate(monkeypatch, isolated_omm_home):
    installed_candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    new_candidate = {
        "repo_id": "org/new",
        "filename": "new.gguf",
        "description": "test",
    }
    artifact = {"candidates": [installed_candidate, new_candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor,
        "rank_candidates",
        lambda artifact, hw: [(installed_candidate, 50.0), (new_candidate, 40.0)],
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            ),
            cli.recommend_status.NOT_INSTALLED,
        ],
    )

    installed = []
    monkeypatch.setattr(cli, "install", lambda ref: installed.append(ref))

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert installed == ["org/new:new.gguf"]


def test_recommend_yes_stops_when_every_candidate_is_installed(
    monkeypatch, isolated_omm_home
):
    candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 50.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "install",
        lambda ref: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )

    result = runner.invoke(cli.app, ["recommend", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "All recommended models are already installed" in result.output


def test_recommend_selecting_installed_candidate_does_not_reinstall(
    monkeypatch, isolated_omm_home
):
    candidate = {
        "repo_id": "org/installed",
        "filename": "installed.gguf",
        "description": "test",
    }
    artifact = {"candidates": [candidate]}

    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(
        cli, "_load_recommendation_with_change_note", lambda config: (artifact, False)
    )
    monkeypatch.setattr(
        cli.predictor, "rank_candidates", lambda artifact, hw: [(candidate, 50.0)]
    )
    monkeypatch.setattr(cli.session_cache, "record_seen", lambda refs: None)
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda candidates: [
            cli.recommend_status.InstallationStatus(
                True, True, ("ollama",), "installed.gguf"
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "_select_recommended_model",
        lambda info, ranked, refs, installations: refs[0],
    )
    monkeypatch.setattr(
        cli,
        "install",
        lambda ref: (_ for _ in ()).throw(AssertionError("must not reinstall")),
    )

    result = runner.invoke(cli.app, ["recommend"])

    assert result.exit_code == 0, result.stdout
    assert "already installed via OMM" in result.output
    assert "omm run installed.gguf" in result.output
