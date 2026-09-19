from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import cli, coding_eval


runner = CliRunner()


def _report():
    return coding_eval.CodingEvaluationReport(
        1,
        "2026-09-17T00:00:00+00:00",
        "model:latest",
        "huggingface",
        "org/model",
        "model-Q4_K_M.gguf",
        "a" * 64,
        "Q4_K_M",
        "ollama",
        "1.0",
        "pack",
        "1",
        "b" * 64,
        (
            coding_eval.CodingTaskResult("g", "generation", "completed", 2, 2, 0.1),
            coding_eval.CodingTaskResult("r", "repair", "failed_tests", 0, 2, 0.1),
        ),
    )


def _patch(monkeypatch):
    monkeypatch.setattr(cli.coding_eval, "find_runtime", lambda: "docker")
    monkeypatch.setattr(cli.coding_eval, "ensure_runtime_available", lambda runtime: None)
    monkeypatch.setattr(cli.coding_eval, "ensure_image_available", lambda runtime, image: None)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.quality_mod,
        "_model_metadata",
        lambda model: {"digest": "sha256:" + "a" * 64, "capabilities": []},
    )
    monkeypatch.setattr(cli.quality_mod, "_model_is_loaded", lambda model: False)
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", lambda model: True)
    monkeypatch.setattr(cli.quality_mod, "ollama_version", lambda: "1.0")
    monkeypatch.setattr(cli.coding_eval, "evaluate_pack", lambda *args, **kwargs: _report())


def test_evaluate_json_is_structured_and_does_not_upload(monkeypatch, isolated_omm_home):
    _patch(monkeypatch)
    monkeypatch.setattr(
        cli.telemetry,
        "send_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("evaluate must not upload")),
    )
    result = runner.invoke(cli.app, ["evaluate", "model:latest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["solved"] == 1
    assert payload["raw_responses_stored"] is False


def test_evaluate_writes_evidence_and_preserves_preloaded_model(monkeypatch, isolated_omm_home, tmp_path):
    _patch(monkeypatch)
    monkeypatch.setattr(cli.quality_mod, "_model_is_loaded", lambda model: True)
    unloaded = []
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", unloaded.append)
    output = tmp_path / "evidence.json"
    result = runner.invoke(cli.app, ["evaluate", "model:latest", "--output", str(output)])
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["model"] == "model:latest"
    assert payload["raw_responses_stored"] is False
    assert unloaded == []


def test_evaluate_fails_before_model_call_without_sandbox(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(
        cli.coding_eval,
        "find_runtime",
        lambda: (_ for _ in ()).throw(coding_eval.SandboxUnavailable("need sandbox")),
    )
    monkeypatch.setattr(
        cli.quality_mod,
        "_model_metadata",
        lambda model: (_ for _ in ()).throw(AssertionError("must not reach model")),
    )
    result = runner.invoke(cli.app, ["evaluate", "model:latest"])
    assert result.exit_code == 1
    assert "need sandbox" in result.stderr
