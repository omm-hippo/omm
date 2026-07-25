"""SSH commit-signature verification for `install.sh` / `omm update`.

Trust anchor: an SSH `allowed_signers` file (see `allowed_signers` next to
this module) shipped as package data, naming the maintainers whose commits
are trusted.

Verifying a freshly fetched commit against the *currently installed* copy
of this file - not the freshly fetched one - is what makes this a chain
rather than a no-op: an attacker with push access could otherwise add
their own key to the same malicious commit and have it self-approve.
Callers must pass the anchor read *before* the fetched commit is checked
out (see `current_trust_anchor` docstring).
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


def verify_commit(
    repo_dir: Path, commit: str, allowed_signers: Path | None
) -> tuple[bool, str]:
    """Verify `commit` in the git repo at `repo_dir` is SSH-signed by a key
    listed in `allowed_signers`.

    `allowed_signers=None` means the currently installed omm predates the
    trust feature - there's no anchor to compare the new commit against
    yet, so this one update is let through unverified (bootstrap). The
    *next* update carries a bundled anchor and starts enforcing the chain.
    """
    if allowed_signers is None:
        return True, "no trust anchor bundled with the current install yet (one-time bootstrap pass-through)"
    if not _git_version_ok():
        return False, "git 2.34+ is required to verify SSH commit signatures"
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
