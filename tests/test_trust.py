import subprocess

import pytest

from omm import trust


def _run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def signing_key(tmp_path):
    key_path = tmp_path / "id_ed25519"
    _run(["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"])
    return key_path


@pytest.fixture
def other_signing_key(tmp_path):
    key_path = tmp_path / "id_ed25519_untrusted"
    _run(["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"])
    return key_path


@pytest.fixture
def allowed_signers(tmp_path, signing_key):
    pub_key = (signing_key.with_suffix(".pub")).read_text().strip()
    path = tmp_path / "allowed_signers"
    path.write_text(f"test@example.com {pub_key}\n")
    return path


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run(["git", "init", "-q"], cwd=repo_dir)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)
    _run(["git", "config", "user.name", "test"], cwd=repo_dir)
    _run(["git", "config", "gpg.format", "ssh"], cwd=repo_dir)
    (repo_dir / "file.txt").write_text("hello\n")
    _run(["git", "add", "file.txt"], cwd=repo_dir)
    return repo_dir


def _commit(repo_dir, message, *, signing_key=None):
    if signing_key is not None:
        _run(["git", "config", "user.signingkey", str(signing_key.with_suffix(".pub"))], cwd=repo_dir)
        _run(["git", "commit", "-q", "-S", "-m", message], cwd=repo_dir)
    else:
        _run(["git", "commit", "-q", "-m", message], cwd=repo_dir)
    return _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).strip()


def test_verify_commit_passes_for_trusted_signer(repo, signing_key, allowed_signers):
    commit = _commit(repo, "signed", signing_key=signing_key)

    ok, message = trust.verify_commit(repo, commit, allowed_signers)

    assert ok, message
    assert commit[:7] in message


def test_verify_commit_fails_for_unsigned(repo, allowed_signers):
    commit = _commit(repo, "unsigned")

    ok, message = trust.verify_commit(repo, commit, allowed_signers)

    assert not ok
    assert "failed signature verification" in message


def test_verify_commit_fails_for_untrusted_signer(repo, other_signing_key, allowed_signers):
    commit = _commit(repo, "signed by stranger", signing_key=other_signing_key)

    ok, message = trust.verify_commit(repo, commit, allowed_signers)

    assert not ok
    assert "failed signature verification" in message


def test_verify_commit_passes_through_when_no_anchor_bundled(repo):
    """An install that predates the trust feature has no anchor to compare
    against yet - allowed through once so the *next* update (now carrying
    an anchor) can start enforcing the chain."""
    commit = _commit(repo, "unsigned")

    ok, message = trust.verify_commit(repo, commit, None)

    assert ok
    assert "bootstrap" in message


def test_git_version_ok_rejects_old_git(monkeypatch):
    class _FakeResult:
        stdout = "git version 2.20.1\n"

    monkeypatch.setattr(trust.subprocess, "run", lambda *a, **k: _FakeResult())

    assert trust._git_version_ok() is False


def test_git_version_ok_accepts_current_git():
    assert trust._git_version_ok() is True


def test_verify_commit_uses_merge_commit_second_parent(repo, signing_key, allowed_signers):
    """Mirrors GitHub's "create a merge commit" strategy: the merge commit
    itself is unsigned (GitHub signs it with its own key in practice), but
    the PR branch tip it wraps carries the contributor's signature."""
    _commit(repo, "base")
    default_branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()
    _run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    (repo / "file.txt").write_text("feature\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    feature_commit = _commit(repo, "feature work", signing_key=signing_key)
    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    _run(["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge feature", "feature"], cwd=repo)
    merge_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    ok, message = trust.verify_commit(repo, merge_commit, allowed_signers)

    assert ok, message
    assert feature_commit[:7] in message


def test_verify_commit_merge_commit_fails_when_second_parent_untrusted(
    repo, other_signing_key, allowed_signers
):
    _commit(repo, "base")
    default_branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()
    _run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    (repo / "file.txt").write_text("feature\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _commit(repo, "feature work by stranger", signing_key=other_signing_key)
    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    _run(["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge feature", "feature"], cwd=repo)
    merge_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    ok, message = trust.verify_commit(repo, merge_commit, allowed_signers)

    assert not ok
    assert "failed signature verification" in message


def test_current_trust_anchor_points_at_bundled_file():
    anchor = trust.current_trust_anchor()

    assert anchor is not None
    assert anchor.name == "allowed_signers"
    assert "ssh-ed25519" in anchor.read_text()
