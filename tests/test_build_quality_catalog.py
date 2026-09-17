from __future__ import annotations

import json

import pytest

from scripts import build_quality_catalog


def report():
    return {
        "schema_version": 1,
        "created_at": "2026-09-17T00:00:00+00:00",
        "model": "model:latest",
        "model_provider": "huggingface",
        "model_repo_id": "org/model",
        "model_filename": "model-Q4_K_M.gguf",
        "model_digest": "a" * 64,
        "quantization": "Q4_K_M",
        "engine": "ollama",
        "engine_version": "1.0",
        "pack": {"id": "coding", "version": "1.0", "sha256": "b" * 64},
        "summary": {
            "score": 0.75,
            "solved": 3,
            "total": 4,
            "by_kind": {
                "generation": {"solved": 2, "total": 2, "tests_passed": 9, "tests_total": 9},
                "repair": {"solved": 1, "total": 2, "tests_passed": 5, "tests_total": 9},
            },
        },
        "tasks": [],
        "raw_responses_stored": False,
    }


def test_builds_valid_official_quality_artifact(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report()), encoding="utf-8")
    artifact = build_quality_catalog.build([source])
    [row] = artifact["evaluations"]
    assert row["score"] == 0.75
    assert row["summary"] == "generation 2/2; repair 1/2"
    assert row["source"] == "official"


def test_refuses_raw_responses_or_missing_identity():
    payload = report()
    payload["raw_responses_stored"] = True
    with pytest.raises(ValueError, match="raw responses"):
        build_quality_catalog.evaluation_from_report(payload)
    payload = report()
    payload["model_digest"] = None
    with pytest.raises(ValueError, match="exact package identity"):
        build_quality_catalog.evaluation_from_report(payload)
