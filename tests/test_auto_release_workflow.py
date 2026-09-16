from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    path = ROOT / ".github" / "workflows" / "auto-release.yml"
    if not path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    return path.read_text(encoding="utf-8")


def test_auto_release_runs_nightly_after_the_retrain_and_on_demand():
    workflow = _workflow()

    assert 'cron: "0 21 * * *"' in workflow
    assert "workflow_dispatch: {}" in workflow
    assert "group: auto-release" in workflow
    assert "cancel-in-progress: false" in workflow


def test_auto_release_pushes_with_the_pat_so_the_release_workflows_fire():
    workflow = _workflow()

    # A GITHUB_TOKEN push would not trigger the tag-driven release workflows.
    assert "token: ${{ secrets.LOCALFIT_RETRAIN_PAT }}" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert 'git push origin "v${VERSION}"' in workflow


def test_auto_release_only_tags_a_moved_green_unpublished_main():
    workflow = _workflow()

    assert 'skip "tag ${tag} already exists"' in workflow
    assert 'skip "main has not moved since ${last_tag}"' in workflow
    assert 'MIN_DAYS_BETWEEN_RELEASES: "3"' in workflow
    assert 'if [ "${GITHUB_EVENT_NAME}" != "workflow_dispatch" ]; then' in workflow
    assert 'if [ "${age_days}" -lt "${MIN_DAYS_BETWEEN_RELEASES}" ]; then' in workflow
    assert "grep -v '^published/'" in workflow
    assert 'skip "only published/ changed since ${last_tag}"' in workflow
    assert "https://pypi.org/pypi/omm-model/${version}/json" in workflow
    assert "https://registry.npmjs.org/@omm-hippo/omm/${version}" in workflow
    assert "/check-runs?per_page=100" in workflow
    assert 'skip "main HEAD has checks that are not green"' in workflow
    assert "if: steps.decide.outputs.release == 'true'" in workflow


def test_auto_release_signs_with_the_trusted_bot_key_and_verifies_before_pushing():
    workflow = _workflow()
    signers = (ROOT / "src" / "omm" / "trust" / "allowed_signers").read_text(encoding="utf-8")

    assert "LOCALFIT_RETRAIN_SSH_KEY: ${{ secrets.LOCALFIT_RETRAIN_SSH_KEY }}" in workflow
    assert 'git config user.email "github-actions[bot]@users.noreply.github.com"' in workflow
    assert "github-actions[bot]@users.noreply.github.com ssh-ed25519" in signers
    assert "git config gpg.format ssh" in workflow
    assert 'git tag -s "v${VERSION}" -m "OMM v${VERSION}"' in workflow
    verify = workflow.index('python scripts/release_artifacts.py verify-release --tag "v${VERSION}"')
    push = workflow.index('git push origin "v${VERSION}"')
    assert verify < push
