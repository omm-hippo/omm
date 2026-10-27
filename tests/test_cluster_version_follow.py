import subprocess

import pytest

from omm import cli, config as config_mod
from omm.cluster import version_follow
from omm.cluster.version_follow import decide

A, B, C = "a" * 40, "b" * 40, "c" * 40


def _v(commit, branch="main", git=True):
    return {"commit": commit, "branch": branch, "git": git}


def test_decide_same_and_update():
    assert decide(_v(B), _v(B), is_ancestor=lambda a, d: True) == "same"
    assert decide(_v(A), _v(B), is_ancestor=lambda a, d: False) == "update"


def test_decide_member_ahead_never_downgrades():
    # The head's commit is an ancestor of the member's: the member is ahead.
    assert decide(_v(B), _v(A), is_ancestor=lambda a, d: (a, d) == (A, B)) == "ahead"


def test_decide_switch_and_manual():
    assert decide(_v(A), _v(B, "beta"), is_ancestor=lambda a, d: False) == "switch"
    assert decide(_v(None, git=False), _v(B), is_ancestor=lambda a, d: False) == "manual"
    assert decide(_v(A), _v(None, git=False), is_ancestor=lambda a, d: False) == "manual"
    assert decide(_v(A), {}, is_ancestor=lambda a, d: False) == "manual"


def _done(code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


@pytest.fixture
def follow(monkeypatch):
    calls = []
    monkeypatch.setattr(version_follow, "local_version", lambda: _v(A))
    monkeypatch.setattr(version_follow, "_is_ancestor", lambda a, d: False)

    def perform(branch, *, same_branch=True, target_commit=None):
        calls.append((branch, same_branch, target_commit))
        return follow.result

    monkeypatch.setattr(cli, "_perform_update", perform)
    follow.result = _done()
    follow.calls = calls
    return follow


def test_apply_updates_to_exact_commit(follow):
    assert version_follow.apply(_v(B)) == ("updated", None)
    assert follow.calls == [("main", True, B)]


def test_apply_reports_blocked_with_first_stderr_line(follow):
    follow.result = _done(1, "commit bbbbbbb is not on main\nmore")
    assert version_follow.apply(_v(B)) == ("blocked", "commit bbbbbbb is not on main")


def test_apply_switch_sets_channel(follow, monkeypatch):
    saved = {}
    monkeypatch.setattr(config_mod, "update_config", lambda **kw: saved.update(kw) or saved)
    assert version_follow.apply(_v(B, "beta")) == ("switched", None)
    assert follow.calls == [("beta", False, B)]
    assert saved == {"update_channel": "beta"}


def test_apply_ahead_and_same_do_nothing(follow, monkeypatch):
    monkeypatch.setattr(version_follow, "_is_ancestor", lambda a, d: True)
    assert version_follow.apply(_v(B)) == ("ahead", None)
    assert version_follow.apply(_v(A)) == ("same", None)
    assert follow.calls == []


class _Git:
    """Answers the git calls `_git_update_src` makes, recording each argv."""

    def __init__(self, ancestor_code=0):
        self.calls = []
        self.ancestor_code = ancestor_code

    def __call__(self, args, *, timeout=30):
        self.calls.append(args)
        if "get-url" in args:
            return subprocess.CompletedProcess(args, 0, stdout=cli._BARE_REPO_URL + "\n", stderr="")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout=A + "\n", stderr="")
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, stdout=B + "\n", stderr="")
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, self.ancestor_code, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


@pytest.fixture
def fake_src(monkeypatch, tmp_path):
    # Never the real ~/.omm/src.
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path / "src")
    verified = []
    monkeypatch.setattr(cli.trust, "verify_update",
                        lambda repo, current, target, anchor, *, same_branch=True:
                        verified.append((current, target, same_branch)) or (True, ""))
    monkeypatch.setattr(cli.trust, "current_trust_anchor", lambda: None)
    return verified


def test_git_update_src_refuses_commit_off_branch(monkeypatch, fake_src):
    git = _Git(ancestor_code=1)
    monkeypatch.setattr(cli, "_run_git", git)
    result = cli._git_update_src("main", target_commit=C)
    assert result.returncode == 1 and "is not on main" in result.stderr
    assert fake_src == []
    assert not any("checkout" in call for call in git.calls)


def test_git_update_src_checks_out_target(monkeypatch, fake_src):
    git = _Git()
    monkeypatch.setattr(cli, "_run_git", git)
    assert cli._git_update_src("main", target_commit=C).returncode == 0
    assert fake_src == [(A, C, True)]
    assert git.calls[-1][-6:] == ["checkout", "-B", "main", C, "--force", "--quiet"]


def test_git_update_src_without_target_is_unchanged(monkeypatch, fake_src):
    git = _Git()
    monkeypatch.setattr(cli, "_run_git", git)
    assert cli._git_update_src("main").returncode == 0
    assert fake_src == [(A, B, True)]
    assert git.calls[-1][-6:] == ["checkout", "-B", "main", "origin/main", "--force", "--quiet"]
    assert not any("merge-base" in call for call in git.calls)
