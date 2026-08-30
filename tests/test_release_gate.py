from __future__ import annotations

import json

import pytest

from scripts import release_gate


def _review(login: str, state: str) -> dict:
    return {"user": {"login": login}, "state": state}


def test_two_distinct_approvers_pass():
    reviews = [_review("a", "APPROVED"), _review("b", "APPROVED")]
    assert release_gate.evaluate(reviews, "author", 2) == {
        "approved": "true",
        "approver_count": "2",
        "approvers": "a,b",
    }


def test_single_approver_is_short():
    out = release_gate.evaluate([_review("a", "APPROVED")], "author", 2)
    assert out == {"approved": "false", "approver_count": "1", "approvers": "a"}


def test_repeat_approval_counts_once():
    reviews = [
        _review("a", "APPROVED"),
        _review("a", "APPROVED"),
        _review("b", "APPROVED"),
    ]
    assert release_gate.approving_reviewers(reviews, "author") == ["a", "b"]


def test_approval_then_changes_requested_drops():
    reviews = [
        _review("a", "APPROVED"),
        _review("a", "CHANGES_REQUESTED"),
        _review("b", "APPROVED"),
    ]
    assert release_gate.approving_reviewers(reviews, "author") == ["b"]


def test_changes_requested_then_approval_counts():
    reviews = [_review("a", "CHANGES_REQUESTED"), _review("a", "APPROVED")]
    assert release_gate.approving_reviewers(reviews, "author") == ["a"]


def test_author_self_approval_excluded():
    reviews = [_review("author", "APPROVED"), _review("b", "APPROVED")]
    assert release_gate.approving_reviewers(reviews, "author") == ["b"]


def test_commented_and_pending_reviews_ignored():
    reviews = [
        _review("a", "APPROVED"),
        _review("a", "COMMENTED"),
        _review("b", "PENDING"),
        _review("b", "COMMENTED"),
    ]
    assert release_gate.approving_reviewers(reviews, "author") == ["a"]


def test_dismissed_approval_not_counted():
    reviews = [_review("a", "APPROVED"), _review("a", "DISMISSED")]
    assert release_gate.approving_reviewers(reviews, "author") == []


def test_malformed_payload_rejected():
    with pytest.raises(ValueError):
        release_gate.latest_standing_by_user({"not": "a list"})
    with pytest.raises(ValueError):
        release_gate.latest_standing_by_user([{"user": {"login": "a"}}])


def test_deleted_account_review_is_skipped():
    reviews = [
        {"user": None, "state": "APPROVED"},
        _review("b", "APPROVED"),
    ]
    assert release_gate.approving_reviewers(reviews, "author") == ["b"]


def test_slurped_pages_are_flattened():
    pages = [
        [_review("a", "APPROVED")],
        [_review("b", "APPROVED")],
    ]
    assert release_gate.flatten_pages(pages) == [
        _review("a", "APPROVED"),
        _review("b", "APPROVED"),
    ]
    assert release_gate.flatten_pages([_review("a", "APPROVED")]) == [
        _review("a", "APPROVED")
    ]


def test_main_writes_output_and_returns_when_satisfied(tmp_path, monkeypatch, capsys):
    reviews = tmp_path / "reviews.json"
    reviews.write_text(
        json.dumps([_review("a", "APPROVED"), _review("b", "APPROVED")]),
        encoding="utf-8",
    )
    gh_output = tmp_path / "gh_output.txt"
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_gate.py",
            "--reviews",
            str(reviews),
            "--author",
            "author",
            "--github-output",
            str(gh_output),
        ],
    )
    release_gate.main()
    written = gh_output.read_text(encoding="utf-8")
    assert "approved=true" in written
    assert "approver_count=2" in written


def test_main_exits_nonzero_when_short(tmp_path, monkeypatch):
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([_review("a", "APPROVED")]), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["release_gate.py", "--reviews", str(reviews), "--author", "author"],
    )
    with pytest.raises(SystemExit) as excinfo:
        release_gate.main()
    assert excinfo.value.code != 0


def test_main_reads_stdin(monkeypatch, capsys):
    payload = json.dumps([_review("a", "APPROVED"), _review("b", "APPROVED")])
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
    monkeypatch.setattr(
        "sys.argv",
        ["release_gate.py", "--reviews", "-", "--author", "author"],
    )
    release_gate.main()
    assert "2 approving reviewer(s)" in capsys.readouterr().out
