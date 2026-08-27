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


def test_verified_install_commit_selects_signed_second_parent(repo, signing_key, allowed_signers):
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

    selected, message = trust.verified_install_commit(repo, merge_commit, allowed_signers)

    assert selected == feature_commit, message
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


def test_verify_commit_rejects_nested_update_branch_merge(repo, signing_key, allowed_signers):
    """An Update branch merge cannot masquerade as a contributor's tip.

    GitHub's final PR merge chooses this nested merge as its second parent.
    It is unsigned even though the commit below it was signed, so the
    one-level resolver must reject it instead of walking back to a signer.
    """
    _commit(repo, "base")
    default_branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()

    _run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    (repo / "file.txt").write_text("feature\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _commit(repo, "signed feature work", signing_key=signing_key)

    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    (repo / "main.txt").write_text("main advanced\n")
    _run(["git", "add", "main.txt"], cwd=repo)
    _commit(repo, "main advanced")

    _run(["git", "checkout", "-q", "feature"], cwd=repo)
    _run(
        ["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "update feature", default_branch],
        cwd=repo,
    )
    nested_merge = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    _run(
        ["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge feature", "feature"],
        cwd=repo,
    )
    candidate = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    assert trust._signing_commit(repo, candidate) == nested_merge
    selected, message = trust.verified_install_commit(repo, candidate, allowed_signers)

    assert selected is None
    assert nested_merge[:7] in message
    assert "failed signature verification" in message


def test_verify_commit_passes_when_merge_commit_itself_is_signed(
    repo, signing_key, allowed_signers
):
    """Regression test: syncing one branch into another with a plain local
    `git merge` (not a GitHub PR merge) produces a 2-parent commit that is
    itself directly SSH-signed by the maintainer. The old one-level
    resolver always redirected 2-parent commits to their second parent,
    so it would ignore this valid signature and instead try to verify
    whatever the second parent happens to be (here, deliberately
    unverifiable) - and fail a perfectly trustworthy commit."""
    _commit(repo, "base")
    default_branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()
    _run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    (repo / "file.txt").write_text("feature\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _commit(repo, "feature work")  # unsigned - must not matter
    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    _run(
        ["git", "config", "user.signingkey", str(signing_key.with_suffix(".pub"))],
        cwd=repo,
    )
    _run(
        ["git", "merge", "-q", "--no-ff", "-S", "-m", "merge feature", "feature"],
        cwd=repo,
    )
    merge_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    ok, message = trust.verify_commit(repo, merge_commit, allowed_signers)

    assert ok, message
    assert merge_commit[:7] in message


def test_verify_update_rejects_signed_parent_with_tampered_merge_result(
    repo, signing_key, allowed_signers
):
    base = _commit(repo, "base")
    default_branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()
    _run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    (repo / "feature.txt").write_text("reviewed\n")
    _run(["git", "add", "feature.txt"], cwd=repo)
    _commit(repo, "signed feature", signing_key=signing_key)
    _run(["git", "checkout", "-q", default_branch], cwd=repo)
    _run(["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge", "feature"], cwd=repo)
    (repo / "malicious.txt").write_text("not present in either parent\n")
    _run(["git", "add", "malicious.txt"], cwd=repo)
    _run(["git", "commit", "-q", "--amend", "--no-gpg-sign", "--no-edit"], cwd=repo)
    target = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    ok, message = trust.verify_update(repo, base, target, allowed_signers)

    assert not ok
    assert "unauthenticated merge-result tree" in message


def test_verify_update_follows_skipped_trust_rotation(
    repo, signing_key, other_signing_key, allowed_signers
):
    base = _commit(repo, "base", signing_key=signing_key)
    new_pub_key = other_signing_key.with_suffix(".pub").read_text().strip()
    anchor_in_repo = repo / trust.TRUST_ANCHOR_REPO_PATH
    anchor_in_repo.parent.mkdir(parents=True)
    anchor_in_repo.write_text(
        allowed_signers.read_text() + f"bot@example.com {new_pub_key}\n"
    )
    _run(["git", "add", trust.TRUST_ANCHOR_REPO_PATH], cwd=repo)
    transition = _commit(repo, "add bot signer", signing_key=signing_key)
    (repo / "file.txt").write_text("generated data\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    target = _commit(repo, "bot update", signing_key=other_signing_key)

    direct_ok, _ = trust.verify_commit(repo, target, allowed_signers)
    ok, message = trust.verify_update(repo, base, target, allowed_signers)

    assert not direct_ok
    assert ok, message
    assert target[:7] in message
    assert "1 trust transition" in message
    assert transition != target


def test_verify_update_rejects_key_that_approves_itself(
    repo, signing_key, other_signing_key, allowed_signers
):
    base = _commit(repo, "base", signing_key=signing_key)
    new_pub_key = other_signing_key.with_suffix(".pub").read_text().strip()
    anchor_in_repo = repo / trust.TRUST_ANCHOR_REPO_PATH
    anchor_in_repo.parent.mkdir(parents=True)
    anchor_in_repo.write_text(
        allowed_signers.read_text() + f"stranger@example.com {new_pub_key}\n"
    )
    _run(["git", "add", trust.TRUST_ANCHOR_REPO_PATH], cwd=repo)
    transition = _commit(repo, "stranger adds itself", signing_key=other_signing_key)
    (repo / "file.txt").write_text("malicious update\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    target = _commit(repo, "stranger update", signing_key=other_signing_key)

    ok, message = trust.verify_update(repo, base, target, allowed_signers)

    assert not ok
    assert transition[:7] in message
    assert "trust chain stopped" in message


def test_verify_update_rejects_replay_of_older_trusted_commit(
    repo, signing_key, allowed_signers
):
    older = _commit(repo, "older signed release", signing_key=signing_key)
    (repo / "file.txt").write_text("security fix\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    installed = _commit(repo, "newer signed release", signing_key=signing_key)

    ok, message = trust.verify_update(repo, installed, older, allowed_signers)

    assert not ok
    assert "older than" in message


def test_verify_update_across_diverged_branches_via_merge_base(
    repo, signing_key, allowed_signers
):
    """Switching update channels between branches that both moved on from a
    shared ancestor (neither is an ancestor of the other) must still verify
    - by walking from their merge-base instead of the installed commit -
    since a GitHub PR merge commit's exact signature never verifies (GitHub
    signs it with its own key), the same way a normal forward update relies
    on the lineage-walk fallback."""
    base = _commit(repo, "base", signing_key=signing_key)

    _run(["git", "checkout", "-q", "-b", "installed-branch"], cwd=repo)
    (repo / "beta.txt").write_text("beta-only work\n")
    _run(["git", "add", "beta.txt"], cwd=repo)
    installed = _commit(repo, "beta-only fix", signing_key=signing_key)

    _run(["git", "checkout", "-q", "-b", "topic", base], cwd=repo)
    (repo / "main.txt").write_text("main-only work\n")
    _run(["git", "add", "main.txt"], cwd=repo)
    topic = _commit(repo, "signed topic work", signing_key=signing_key)

    _run(["git", "checkout", "-q", "-b", "main-line", base], cwd=repo)
    _run(["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge topic", "topic"], cwd=repo)
    target = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    assert trust._merge_base(repo, installed, target) == base
    assert trust._is_ancestor(repo, installed, target) is False
    assert trust._is_ancestor(repo, target, installed) is False

    direct_ok, _ = trust.verify_commit(repo, target, allowed_signers)
    ok, message = trust.verify_update(repo, installed, target, allowed_signers)

    assert not direct_ok
    assert ok, message
    assert target[:7] in message


def test_verify_update_rejects_unrelated_history(repo, signing_key, allowed_signers):
    installed = _commit(repo, "installed release", signing_key=signing_key)
    _run(["git", "checkout", "-q", "--orphan", "unrelated"], cwd=repo)
    (repo / "file.txt").write_text("unrelated\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    target = _commit(repo, "unrelated history")  # unsigned: must not verify directly

    ok, message = trust.verify_update(repo, installed, target, allowed_signers)

    assert not ok
    assert "no common history" in message


def test_current_trust_anchor_points_at_bundled_file():
    anchor = trust.current_trust_anchor()

    assert anchor is not None
    assert anchor.name == "allowed_signers"
    assert "ssh-ed25519" in anchor.read_text()
