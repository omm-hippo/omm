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
from tempfile import TemporaryDirectory

MIN_GIT_VERSION = (2, 34)  # first release with SSH commit-signature support
TRUST_ANCHOR_REPO_PATH = "src/omm/trust/allowed_signers"


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


def verify_update(
    repo_dir: Path,
    current_commit: str | None,
    target_commit: str,
    allowed_signers: Path | None,
) -> tuple[bool, str]:
    """Verify an update while safely following skipped trust rotations.

    The common case remains a single verification of ``target_commit``
    against the anchor bundled with the running install.  If that fails,
    and the current commit is an ancestor of the target, inspect only the
    first-parent commits that changed :data:`TRUST_ANCHOR_REPO_PATH`.
    Each anchor-changing commit must be accepted by the anchor that came
    before it; only then is its new anchor used for the next transition and
    finally for the target.

    This lets an infrequently updated client cross a maintainer-approved key
    rotation without letting a target commit add a key and approve itself.
    Non-ancestral channel switches retain ``verify_commit``'s direct-target
    behavior.
    """
    ok, direct_message = verify_commit(repo_dir, target_commit, allowed_signers)
    if ok or allowed_signers is None or not current_commit:
        return ok, direct_message

    try:
        ancestor = subprocess.run(
            [
                "git", "-C", str(repo_dir), "merge-base", "--is-ancestor",
                current_commit, target_commit,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, direct_message
    if ancestor.returncode != 0:
        return False, direct_message

    try:
        changed = subprocess.run(
            [
                "git", "-C", str(repo_dir), "log", "--first-parent",
                "--reverse", "--format=%H",
                f"{current_commit}..{target_commit}", "--",
                TRUST_ANCHOR_REPO_PATH,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, direct_message
    if changed.returncode != 0:
        return False, direct_message

    transitions = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
    if not transitions:
        return False, direct_message

    candidates = list(transitions)
    if candidates[-1] != target_commit:
        candidates.append(target_commit)
    transition_set = set(transitions)

    try:
        original_anchor = allowed_signers.read_bytes()
    except OSError as exc:
        return False, f"could not read current trust anchor: {exc}"

    with TemporaryDirectory(prefix="omm-trust-") as tmp:
        evolving_anchor = Path(tmp) / "allowed_signers"
        evolving_anchor.write_bytes(original_anchor)

        for candidate in candidates:
            verified, message = verify_commit(repo_dir, candidate, evolving_anchor)
            if not verified:
                return False, f"update trust chain stopped at {candidate[:7]}: {message}"
            if candidate not in transition_set:
                continue
            try:
                anchor_at_commit = subprocess.run(
                    [
                        "git", "-C", str(repo_dir), "show",
                        f"{candidate}:{TRUST_ANCHOR_REPO_PATH}",
                    ],
                    capture_output=True,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                return False, f"reading trust anchor from {candidate[:7]} timed out"
            if anchor_at_commit.returncode != 0 or not anchor_at_commit.stdout.strip():
                detail = anchor_at_commit.stderr.decode(errors="replace").strip()
                return False, f"trusted commit {candidate[:7]} has no usable trust anchor: {detail}"
            evolving_anchor.write_bytes(anchor_at_commit.stdout)

    return True, f"commit {target_commit[:7]} signature verified through {len(transitions)} trust transition(s)"
