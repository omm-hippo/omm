"""The arena aggregation workflow's text contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/arena-aggregate.yml")


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_exists(text):
    assert text.strip()


def test_it_runs_nightly_and_can_be_dispatched(text):
    assert 'cron: "0 4 * * *"' in text
    assert "workflow_dispatch" in text


def test_it_does_not_share_the_train_workflow_hour(text):
    # train.yml runs at 03:00 and opens an auto-merging PR of its own.
    assert 'cron: "0 3 * * *"' not in text


def test_a_dispatch_queues_behind_the_cron_run(text):
    assert "group: arena-aggregate" in text
    assert "cancel-in-progress: false" in text


def test_every_action_is_pinned_to_a_commit_sha(text):
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    for reference in uses:
        assert re.search(r"@[0-9a-f]{40}$", reference), reference


def test_the_credential_is_passed_through_the_environment(text):
    assert "LOCALFIT_VOTES_ADMIN_TOKEN" in text
    # Never interpolated into the URL: it would land in the run log.
    assert "${{ secrets.LOCALFIT_VOTES_ADMIN_TOKEN }}?" not in text
    assert "access_token=" not in text


def test_signing_is_gated_on_the_quality_gate_and_a_real_change(text):
    assert "gate_status == 'passed'" in text
    assert "artifact_changed == 'true'" in text


def test_only_the_two_artifact_paths_are_staged(text):
    assert "git add published/arena-leaderboard.json" in text
    assert "published/arena-leaderboard.manifest.json" in text
    assert "git add -A" not in text
    assert "git add ." not in text


def test_the_pr_head_is_ssh_signed_so_trusted_head_passes(text):
    assert "LOCALFIT_RETRAIN_SSH_KEY" in text
    assert "gpg.format ssh" in text
    assert "commit.gpgsign true" in text


def test_it_never_pushes_straight_to_main(text):
    assert "gh pr create" in text
    assert "git push origin main" not in text


def test_the_pr_body_satisfies_the_korean_description_check(text):
    # CI check "PR 설명 확인" runs on bot PRs too.
    import os
    import subprocess
    import sys
    import textwrap

    # The heredoc sits inside a `run: |` block, so every line still carries the
    # YAML block indentation that the runner strips before bash sees it.
    match = re.search(
        r"PR_BODY=\$\(cat <<'BODY'\n(.*?)\n[ \t]*BODY\n", text, re.DOTALL
    )
    assert match, "the workflow must build PR_BODY from a literal heredoc"
    body = textwrap.dedent(match.group(1))
    assert not body.startswith(" "), "the heredoc body must dedent to column 0"
    completed = subprocess.run(
        [sys.executable, "scripts/check_pr_description.py"],
        capture_output=True,
        text=True,
        env={**os.environ, "PR_BODY": body, "PR_AUTHOR": "minigu5"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
