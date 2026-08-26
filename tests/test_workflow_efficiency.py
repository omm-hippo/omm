from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_WORKFLOWS = [
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "npm-package.yml",
    *sorted((ROOT / ".github" / "workflows").glob("ci-engine-*.yml")),
]


def test_validation_pushes_run_only_on_long_lived_branches():
    expected = '  push:\n    branches:\n      - "main"\n      - "beta"\n'
    for workflow in VALIDATION_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert expected in text, workflow


def test_general_and_engine_validation_cancel_stale_runs():
    workflows = [
        ROOT / ".github" / "workflows" / "ci.yml",
        *sorted((ROOT / ".github" / "workflows").glob("ci-engine-*.yml")),
    ]
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "group: validation-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}" in text
        assert "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}" in text
