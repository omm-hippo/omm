from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo


runner = CliRunner()


def _candidate(name: str, size_gb: float, tags=None) -> dict:
    return {
        "name": name,
        "repo_id": f"org/{name}",
        "filename": f"{name}-Q4_K_M.gguf",
        "size_bytes": int(size_gb * 1024**3),
        "pipeline_tag": "text-generation",
        "tags": tags or [],
    }


def _hardware() -> HardwareInfo:
    return HardwareInfo("macOS", "", "Apple M5", 24, 8, True, "Apple M5", 24, 8)


def _patch(monkeypatch):
    one = _candidate("One-7B", 4, ["coding"])
    two = _candidate("Two-12B", 8)
    artifact = {"trees": [{}], "candidates": [one, two]}
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_load_recommendation_with_change_note", lambda config: (artifact, False))
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [(one, 30.0), (two, 15.0)])
    monkeypatch.setattr(
        cli.recommend_status,
        "detect_installation_statuses",
        lambda selected: [cli.recommend_status.NOT_INSTALLED] * len(selected),
    )
    return one, two


def test_compare_json_is_read_only_and_explains_incomplete_quality(monkeypatch, isolated_omm_home):
    _patch(monkeypatch)
    result = runner.invoke(
        cli.app,
        ["compare", "One-7B", "Two-12B", "--profile", "balanced", "--for", "coding", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["purpose"] == "Coding"
    assert payload["quality_complete"] is False
    assert payload["best_fit_ref"].endswith("Two-12B-Q4_K_M.gguf")
    assert payload["fastest_ref"].endswith("One-7B-Q4_K_M.gguf")
    assert all(row["measured_quality"] is None for row in payload["models"])


def test_compare_text_labels_hardware_winners(monkeypatch, isolated_omm_home):
    _patch(monkeypatch)
    result = runner.invoke(cli.app, ["compare", "One-7B", "Two-12B"])
    assert result.exit_code == 0, result.output
    assert "BEST FIT" in result.stdout
    assert "FASTEST" in result.stdout
    assert "did not download, install, or run" in result.stdout


def test_compare_rejects_unknown_before_install_state(monkeypatch, isolated_omm_home):
    _patch(monkeypatch)
    called = []
    monkeypatch.setattr(cli.recommend_status, "detect_installation_statuses", lambda selected: called.append(selected))
    result = runner.invoke(cli.app, ["compare", "Missing", "One-7B"])
    assert result.exit_code == 2
    assert "not in the signed recommendation catalog" in result.stderr
    assert called == []


def test_compare_uses_complete_signed_quality_evidence(monkeypatch, isolated_omm_home):
    one, two = _patch(monkeypatch)
    monkeypatch.setattr(
        cli.quality_catalog,
        "load_cached",
        lambda public_key: {
            cli.compare_mod.candidate_key(one): [
                cli.compare_mod.QualityEvidence("Coding", "coding", "1", 0.7, "7/10")
            ],
            cli.compare_mod.candidate_key(two): [
                cli.compare_mod.QualityEvidence("Coding", "coding", "1", 0.9, "9/10")
            ],
        },
    )
    monkeypatch.setattr(cli, "load_config", lambda: {"catalog_public_key": "key"})
    result = runner.invoke(cli.app, ["compare", "One-7B", "Two-12B", "--for", "coding", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["quality_complete"] is True
    assert payload["best_measured_quality_ref"].endswith("Two-12B-Q4_K_M.gguf")
    assert payload["best_match_ref"] == payload["best_measured_quality_ref"]
