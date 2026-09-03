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

# Every `subprocess.run` in this module decodes with these. git writes its
# porcelain output and its i18n messages as UTF-8 on every platform, so this
# is the *correct* decoding rather than a guess - unlike the interpreter's
# locale default, which is cp949 on a Korean Windows box and raised
# UnicodeDecodeError mid-`omm update` (the bug PR #127 fixed in linker.py).
#
# `errors="replace"` cannot weaken verification here, and that is deliberate:
# no trust decision in this module is made from decoded text. The pass/fail
# verdict is always `returncode` (git verify-commit, merge-base
# --is-ancestor), and the only text ever compared or fed back into git is
# `%H`/`rev-list` output - 40-character hex, pure ASCII, which round-trips
# byte-identically under any ASCII-compatible codec and so can never acquire
# a U+FFFD. Decoded text reaches nothing but the human-readable `detail`
# string in a failure message. Raising instead of replacing would only turn a
# cosmetically odd byte in a git error message into a crash, which is exactly
# the failure mode being removed.
#
# The kwargs are spelled out at every call rather than shared through a dict
# so that tests/test_subprocess_encoding_guard.py can see them in the AST.


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
            ["git", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    # "git version 2.43.0" (sometimes "2.43.0.windows.1" etc.)
    parts = result.stdout.split()
    if getattr(result, "returncode", 0) != 0 or len(parts) < 3:
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
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--parents", "-n", "1", commit],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return commit
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
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, f"signature verification of {commit[:7]} timed out"
    except OSError as error:
        return False, f"signature verification of {commit[:7]} could not run: {error}"
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

    This primitive verifies the exact commit only. A signature on a parent
    does not authenticate the child's tree; callers that install a PR head
    may use :func:`verified_install_commit` to select the signed parent, and
    updates use :func:`verify_update` to validate every merge result.
    """
    if allowed_signers is None:
        return True, "no trust anchor bundled with the current install yet (one-time bootstrap pass-through)"
    if not _git_version_ok():
        return False, "git 2.34+ is required to verify SSH commit signatures"
    return _verify_signature(repo_dir, commit, allowed_signers)


def verified_install_commit(
    repo_dir: Path, commit: str, allowed_signers: Path | None
) -> tuple[str | None, str]:
    """Return the exact signed commit that a fresh install may execute."""
    ok, message = verify_commit(repo_dir, commit, allowed_signers)
    if ok:
        return commit, message
    if allowed_signers is None:
        return commit, message
    target = _signing_commit(repo_dir, commit)
    if target == commit:
        return None, message
    ok, parent_message = _verify_signature(repo_dir, target, allowed_signers)
    return (target if ok else None), parent_message


def _parents(repo_dir: Path, commit: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "show", "-s", "--format=%P", commit],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return result.stdout.split() if result.returncode == 0 else []


def _deterministic_merge_tree_matches(repo_dir: Path, commit: str, parents: list[str]) -> bool:
    if len(parents) != 2:
        return False
    try:
        target_tree = subprocess.run(
            ["git", "-C", str(repo_dir), "show", "-s", "--format=%T", commit],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        merged_tree = subprocess.run(
            ["git", "-C", str(repo_dir), "merge-tree", "--write-tree", *parents],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    merged_lines = merged_tree.stdout.splitlines()
    return (
        target_tree.returncode == 0
        and merged_tree.returncode == 0
        and bool(merged_lines)
        and target_tree.stdout.strip() == merged_lines[0].strip()
    )


def _verify_lineage_commit(
    repo_dir: Path, commit: str, expected_first_parent: str, allowed_signers: Path
) -> tuple[bool, str]:
    exact, exact_message = _verify_signature(repo_dir, commit, allowed_signers)
    if exact:
        return True, exact_message
    parents = _parents(repo_dir, commit)
    if len(parents) != 2 or parents[0] != expected_first_parent:
        return False, exact_message
    signed, signed_message = _verify_signature(repo_dir, parents[1], allowed_signers)
    if not signed:
        return False, signed_message
    if not _deterministic_merge_tree_matches(repo_dir, commit, parents):
        return False, f"merge commit {commit[:7]} has an unauthenticated merge-result tree"
    return True, f"merge commit {commit[:7]} deterministically matches signed parent {parents[1][:7]}"


def _is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool | None:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_dir), "merge-base", "--is-ancestor",
                ancestor, descendant,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _merge_base(repo_dir: Path, a: str, b: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "merge-base", a, b],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def verify_update(
    repo_dir: Path,
    current_commit: str | None,
    target_commit: str,
    allowed_signers: Path | None,
    *,
    same_branch: bool = True,
) -> tuple[bool, str]:
    """Verify an update while safely following skipped trust rotations.

    An exact target signature authenticates its tree directly, except that an
    already-installed descendant cannot be silently rolled back to an older
    signed ancestor while `same_branch` is True - the case for `omm update`,
    where nothing but a compromised fetch could make "latest" move backward,
    so any such move must be rejected outright. `same_branch=False` is for an
    explicit, user-requested channel switch to a different branch: if the new
    target turns out to be an ancestor of the installed commit, its content
    was already checked out and verified at some earlier point on the way to
    the installed commit (git's hash-chained history makes that commit's
    bytes exactly what they always were), so there is nothing left to verify
    - accept it directly rather than reject a deliberate, informed downgrade.

    Otherwise, every commit on the first-parent path from a trusted base
    commit is verified. That base is the installed commit itself when it is
    an ancestor of the target (the common case: moving forward on one
    branch); when the target sits on a sibling branch instead (e.g.
    switching update channels between branches that have diverged), the base
    is their merge-base, since that commit was already part of the installed
    commit's verified history. Unsigned two-parent merges are accepted only
    when their second parent is trusted and ``git merge-tree`` reproduces the
    exact target tree.
    """
    if current_commit and target_commit == current_commit:
        # Nothing to update: the target is the commit already checked out
        # and running. Re-verifying it would reject the common case where
        # it is an unsigned two-parent merge that the lineage walk below
        # accepted when it was installed - the walk from a commit to
        # itself is empty, so it used to fail a no-op `omm update`.
        return True, f"commit {target_commit[:7]} is already the installed commit"
    ok, direct_message = verify_commit(repo_dir, target_commit, allowed_signers)
    if current_commit and target_commit != current_commit:
        target_is_ancestor = _is_ancestor(repo_dir, target_commit, current_commit)
        if target_is_ancestor is None:
            return False, "could not compare target and installed commit ancestry"
        if target_is_ancestor:
            if not same_branch:
                return (
                    True,
                    f"commit {target_commit[:7]} already verified as part of the "
                    "installed history",
                )
            return False, "target commit is older than the installed commit"
    if ok or allowed_signers is None:
        return ok, direct_message
    if not current_commit:
        return False, direct_message
    if _is_ancestor(repo_dir, current_commit, target_commit) is True:
        base = current_commit
    else:
        base = _merge_base(repo_dir, current_commit, target_commit)
        if base is None:
            return False, "current and target commits share no common history"

    try:
        lineage = subprocess.run(
            [
                "git", "-C", str(repo_dir), "rev-list", "--first-parent",
                "--reverse", f"{base}..{target_commit}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, direct_message
    if lineage.returncode != 0:
        return False, direct_message
    candidates = [line.strip() for line in lineage.stdout.splitlines() if line.strip()]
    if not candidates or candidates[-1] != target_commit:
        return False, direct_message

    try:
        original_anchor = allowed_signers.read_bytes()
    except OSError as exc:
        return False, f"could not read current trust anchor: {exc}"

    with TemporaryDirectory(prefix="omm-trust-") as tmp:
        evolving_anchor = Path(tmp) / "allowed_signers"
        evolving_anchor.write_bytes(original_anchor)

        # `base` (the installed commit) and everything in its history are
        # trusted. When `base` is an ancestor of `target` but sits on a
        # second-parent side branch rather than `target`'s first-parent
        # trunk - the normal shape right after a fresh install, whose
        # checkout is the signed PR-tip (a merge's *second* parent), not the
        # merge commit itself - the oldest lineage commit's first parent is
        # not `base` but the trunk commit where `base`'s history last met the
        # trunk. Seed from there; requiring `parents[0] == base` would stop
        # the walk dead on the first trunk merge and misreport that merge's
        # (GitHub-signed, unverifiable) signature as the failure.
        oldest_parents = _parents(repo_dir, candidates[0])
        trunk_base = oldest_parents[0] if oldest_parents else base
        if trunk_base != base and _is_ancestor(repo_dir, trunk_base, base) is not True:
            return False, (
                f"lineage base {trunk_base[:7]} is not part of the installed "
                "commit's trusted history"
            )

        previous = trunk_base
        transitions = 0
        for candidate in candidates:
            verified, message = _verify_lineage_commit(
                repo_dir, candidate, previous, evolving_anchor
            )
            if not verified:
                return False, f"update trust chain stopped at {candidate[:7]}: {message}"
            try:
                changed_anchor = subprocess.run(
                    [
                        "git", "-C", str(repo_dir), "diff", "--quiet",
                        previous, candidate, "--", TRUST_ANCHOR_REPO_PATH,
                    ],
                    capture_output=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False, f"could not inspect trust anchor change at {candidate[:7]}"
            previous = candidate
            if changed_anchor.returncode == 0:
                continue
            if changed_anchor.returncode != 1:
                return False, f"could not inspect trust anchor change at {candidate[:7]}"
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
            except OSError as error:
                return False, f"reading trust anchor from {candidate[:7]} failed: {error}"
            if anchor_at_commit.returncode != 0 or not anchor_at_commit.stdout.strip():
                detail = anchor_at_commit.stderr.decode(errors="replace").strip()
                return False, f"trusted commit {candidate[:7]} has no usable trust anchor: {detail}"
            evolving_anchor.write_bytes(anchor_at_commit.stdout)
            transitions += 1

    return True, f"commit {target_commit[:7]} signature verified through {transitions} trust transition(s)"
