"""SSH commit-signature verification for `install.sh` / `install.ps1` / `omm update`.

Trust anchor: an SSH `allowed_signers` file (see `allowed_signers` next to
this module) shipped as package data, naming the maintainers whose commits
are trusted.

Verifying a freshly fetched commit against the *currently installed* copy
of this file - not the freshly fetched one - is what makes this a chain
rather than a no-op: an attacker with push access could otherwise add
their own key to the same malicious commit and have it self-approve.
Callers must pass the anchor read *before* the fetched commit is checked
out (see `current_trust_anchor` docstring).

CAUTION for future edits to `verify_commit`/`_signing_commit`: every
already-installed `omm` runs *its own* copy of this module against the
*newly fetched* commit - there is no way to ship a change to the
verification algorithm itself through the channel it verifies. An
already-installed client can only pass a newly fetched commit if the OLD
algorithm (bundled in that client) already accepts it. Changing what
"a valid signed commit" means (as `_signing_commit`'s merge-commit
resolution did) therefore strands every existing install at the next
commit that only the NEW algorithm would accept, until each of them is
bridged past it once by hand (see the PR #4/#5 incident write-up). Prefer
changes that keep old algorithms accepting new commits; if that's not
possible, the fix must ship in `install.sh`/`install.ps1` too (the TOFU
path has no "old install" to be stuck behind) and the stranding needs
calling out loudly in release notes.
"""

from __future__ import annotations

import subprocess
from importlib.resources import files
from pathlib import Path

MIN_GIT_VERSION = (2, 34)  # first release with SSH commit-signature support


def current_trust_anchor() -> Path | None:
    """Path to the `allowed_signers` file baked into the *currently
    running* omm install, or None if this install predates the trust
    feature - see the module docstring in this package's design doc for
    why that's a one-time pass-through rather than a failure."""
    path = Path(str(files("omm").joinpath("trust/allowed_signers")))
    return path if path.is_file() else None


def _git_version_ok() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    # "git version 2.43.0" (sometimes "2.43.0.windows.1" etc.)
    parts = result.stdout.split()
    if len(parts) < 3:
        return False
    try:
        major, minor = (int(p) for p in parts[2].split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= MIN_GIT_VERSION


def _signing_commit(repo_dir: Path, commit: str) -> str:
    """The commit whose signature actually matters for trust purposes.

    The repo's branch protection only allows landing changes through a PR,
    merged with the "create a merge commit" strategy. GitHub builds that
    merge commit itself and signs it with GitHub's own key - the
    contributor's signature lives on the merge commit's second parent (the
    PR branch tip) instead. For a normal two-parent merge commit, that's
    the commit to verify; anything else (a direct single-parent commit, or
    an octopus merge) is verified as-is.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-list", "--parents", "-n", "1", commit],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return commit
    parts = result.stdout.split()
    if len(parts) == 3:  # commit + exactly two parents
        return parts[2]
    return commit


def _verify_signature(
    repo_dir: Path, commit: str, allowed_signers: Path
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-c", "gpg.format=ssh",
                "-c", f"gpg.ssh.allowedSignersFile={allowed_signers}",
                "-C", str(repo_dir),
                "verify-commit", commit,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, f"signature verification of {commit[:7]} timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"commit {commit[:7]} failed signature verification: {detail}"
    return True, f"commit {commit[:7]} signature verified"


def verify_commit(
    repo_dir: Path, commit: str, allowed_signers: Path | None
) -> tuple[bool, str]:
    """Verify `commit` in the git repo at `repo_dir` is SSH-signed by a key
    listed in `allowed_signers`.

    `allowed_signers=None` means the currently installed omm predates the
    trust feature - there's no anchor to compare the new commit against
    yet, so this one update is let through unverified (bootstrap). The
    *next* update carries a bundled anchor and starts enforcing the chain.

    Tries `commit` itself first - a maintainer can directly SSH-sign a
    merge commit (e.g. syncing one branch into another outside GitHub's PR
    flow), and that signature is trustworthy on its own. Only when that
    fails and `commit` has exactly two parents does this fall back to
    `_signing_commit`'s second-parent heuristic, for the GitHub
    "create a merge commit" case where GitHub itself signs the merge with
    a key this repo doesn't trust and the real signature lives one hop
    down, on the PR branch tip.
    """
    if allowed_signers is None:
        return True, "no trust anchor bundled with the current install yet (one-time bootstrap pass-through)"
    if not _git_version_ok():
        return False, "git 2.34+ is required to verify SSH commit signatures"
    ok, message = _verify_signature(repo_dir, commit, allowed_signers)
    if ok:
        return ok, message
    target = _signing_commit(repo_dir, commit)
    if target == commit:
        return ok, message
    return _verify_signature(repo_dir, target, allowed_signers)
