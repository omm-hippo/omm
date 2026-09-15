import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
VALIDATION_WORKFLOWS = [
    WORKFLOW_ROOT / "ci.yml",
    WORKFLOW_ROOT / "npm-package.yml",
    *sorted(WORKFLOW_ROOT.glob("ci-engine-*.yml")),
]


def _require_workflow_sources() -> None:
    if not (WORKFLOW_ROOT / "ci.yml").is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")


def test_missing_workflow_sources_are_skipped(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "WORKFLOW_ROOT", tmp_path / "missing")
    with pytest.raises(pytest.skip.Exception):
        _require_workflow_sources()


def test_validation_pushes_run_only_on_long_lived_branches():
    _require_workflow_sources()
    expected = '  push:\n    branches:\n      - "main"\n      - "beta"\n'
    for workflow in VALIDATION_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert expected in text, workflow


def test_general_and_engine_validation_cancel_stale_runs():
    _require_workflow_sources()
    workflows = [
        WORKFLOW_ROOT / "ci.yml",
        *sorted(WORKFLOW_ROOT.glob("ci-engine-*.yml")),
    ]
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "group: validation-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}" in text
        assert "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}" in text


_PINNED = re.compile(r"^\s*(?:-\s*)?(?:uses:)\s+(\S+)", re.M)


def test_every_workflow_pins_actions_to_a_sha():
    _require_workflow_sources()
    offenders = []
    for workflow in sorted(WORKFLOW_ROOT.glob("*.yml")):
        for ref in _PINNED.findall(workflow.read_text(encoding="utf-8")):
            if ref.startswith("./"):        # local reusable workflow
                continue
            if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref):
                offenders.append(f"{workflow.name}: {ref}")
    assert offenders == []
