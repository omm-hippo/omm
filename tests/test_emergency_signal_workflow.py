"""The emergency-signal workflow opens a PR and only *schedules* auto-merge
(`gh pr merge --auto --merge`) - main still requires 1 approving review, so
the run must not read as a silent success. See docs/superpowers for the
s7-ci-01 decision (option B: keep human approval, surface the blocker
instead of adding an auto-approve step)."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read_workflow() -> str:
    workflow_path = ROOT / ".github" / "workflows" / "emergency-signal.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    return workflow_path.read_text(encoding="utf-8")


def test_emergency_signal_workflow_warns_that_the_pr_still_needs_review():
    workflow = _read_workflow()

    assert "::warning::" in workflow

    merge_line = next(
        i for i, line in enumerate(workflow.splitlines()) if "gh pr merge" in line
    )
    warning_line = next(
        i for i, line in enumerate(workflow.splitlines()) if "::warning::" in line
    )
    assert warning_line > merge_line


def test_emergency_signal_workflow_does_not_auto_approve():
    workflow = _read_workflow()

    assert "gh pr review" not in workflow
    assert "--approve" not in workflow
