from __future__ import annotations

import json

import pytest

from scripts import retrain_decision


@pytest.mark.parametrize(
    ("report", "expected_status", "expected_title"),
    [
        (
            {"passed": True},
            "passed",
            "chore: nightly recommendation model retrain",
        ),
        (
            {"passed": False},
            "rejected",
            "chore: refresh candidate pool after rejected retrain",
        ),
        (
            {"passed": False, "skipped": True},
            "skipped",
            "chore: refresh candidate pool after skipped retrain",
        ),
    ],
)
def test_publication_outputs_match_quality_gate_status(
    report,
    expected_status,
    expected_title,
):
    outputs = retrain_decision.publication_outputs(report, b"same", b"same")

    assert outputs["quality_gate_status"] == expected_status
    assert outputs["model_changed"] == "false"
    assert outputs["pr_title"] == expected_title


def test_passed_candidate_may_change_the_published_model():
    outputs = retrain_decision.publication_outputs(
        {"passed": True},
        b"candidate",
        b"incumbent",
    )

    assert outputs["quality_gate_status"] == "passed"
    assert outputs["model_changed"] == "true"
    assert "passed the quality gate" in outputs["pr_body"]


@pytest.mark.parametrize(
    "report",
    [
        {"passed": False},
        {"passed": False, "skipped": True},
    ],
)
def test_nonpassing_candidate_cannot_change_the_published_model(report):
    with pytest.raises(ValueError, match="changed the published model"):
        retrain_decision.publication_outputs(report, b"candidate", b"incumbent")


@pytest.mark.parametrize(
    "report",
    [
        [],
        {},
        {"passed": "false"},
        {"passed": False, "skipped": "true"},
        {"passed": True, "skipped": True},
    ],
)
def test_quality_report_schema_is_strict(report):
    with pytest.raises(ValueError):
        retrain_decision.quality_gate_status(report)


def test_missing_candidate_file_is_an_error(tmp_path):
    report = tmp_path / "report.json"
    incumbent = tmp_path / "incumbent.json"
    report.write_text(json.dumps({"passed": False}), encoding="utf-8")
    incumbent.write_bytes(b"same")

    with pytest.raises(ValueError, match="could not inspect retrain result"):
        retrain_decision.inspect_files(report, tmp_path / "missing.json", incumbent)


def test_github_outputs_are_written_without_reinterpreting_values(tmp_path):
    destination = tmp_path / "github-output"
    outputs = retrain_decision.publication_outputs(
        {"passed": False},
        b"same",
        b"same",
    )

    retrain_decision.append_github_outputs(destination, outputs)

    written = dict(
        line.split("=", maxsplit=1)
        for line in destination.read_text(encoding="utf-8").splitlines()
    )
    assert written == outputs
