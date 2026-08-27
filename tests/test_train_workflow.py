from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_train_workflow_reports_the_actual_quality_gate_outcome():
    workflow_path = ROOT / ".github" / "workflows" / "train.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "id: train" in workflow
    assert "python scripts/retrain_decision.py" in workflow
    assert "steps.train.outputs.quality_gate_status == 'passed'" in workflow
    assert "steps.train.outputs.model_changed == 'true'" in workflow
    assert "QUALITY_GATE_STATUS: ${{ steps.train.outputs.quality_gate_status }}" in workflow
    assert "MODEL_CHANGED: ${{ steps.train.outputs.model_changed }}" in workflow
    assert "COMMIT_MESSAGE: ${{ steps.train.outputs.commit_message }}" in workflow
    assert "PR_TITLE: ${{ steps.train.outputs.pr_title }}" in workflow
    assert "PR_BODY: ${{ steps.train.outputs.pr_body }}" in workflow
    assert 'git commit -m "$COMMIT_MESSAGE"' in workflow
    assert '--title "$PR_TITLE"' in workflow
    assert '--body "$PR_BODY"' in workflow


def test_train_workflow_does_not_resign_or_publish_a_changed_model_after_rejection():
    workflow_path = ROOT / ".github" / "workflows" / "train.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert (
        'if [ "$QUALITY_GATE_STATUS" != "passed" ] && [ "$MODEL_CHANGED" != "false" ]; then'
        in workflow
    )
    assert "Quality gate reported $QUALITY_GATE_STATUS but changed the published model." in workflow
    assert '--title "chore: nightly recommendation model retrain"' not in workflow
    assert (
        '--body "Automated nightly retrain. Candidate passed the quality gate'
        not in workflow
    )


def test_train_workflow_preserves_only_the_redacted_plausibility_report():
    workflow_path = ROOT / ".github" / "workflows" / "train.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert '--plausibility-report "$RUNNER_TEMP/telemetry-plausibility-report.json"' in workflow
    assert "name: telemetry-plausibility-audit" in workflow
    assert "telemetry-plausibility-report.json" in workflow
    assert "telemetry.json" not in workflow
    assert "retention-days: 30" in workflow
