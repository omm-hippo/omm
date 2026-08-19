#!/usr/bin/env python3
"""Verify a candidate commit with a verifier and SSH anchor supplied by CI.

This file deliberately uses only the standard library and must be run from a
trusted base checkout.  In particular, it must never import ``omm.trust``
from the candidate checkout: a PR could otherwise change the verifier or the
allowed-signers file that is meant to constrain it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


MIN_GIT_VERSION = (2, 34)


def _run(args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    try:
        # git writes UTF-8 on every platform; see the note in src/omm/trust
        # for why errors="replace" cannot weaken a verification decision.
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(error))


def _git_version_ok() -> bool:
    result = _run(["git", "--version"], timeout=5)
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) < 3:
        return False
    try:
        major, minor = (int(part) for part in parts[2].split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= MIN_GIT_VERSION


def _resolve_commit(repo: Path, commit: str) -> tuple[str | None, str | None]:
    result = _run(
        ["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", commit], timeout=10
    )
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()
    parts = result.stdout.split()
    if not parts:
        return None, "git did not resolve the candidate commit"
    # This gate validates a PR's exact head SHA, not a GitHub-generated main
    # merge commit. Never peel a parent here: an unsigned malicious merge
    # could otherwise put a trusted signed commit in parent 2 and pass.
    return parts[0], None


def verify(repo: Path, commit: str, anchor: Path) -> tuple[bool, str]:
    # git evaluates gpg.ssh.allowedSignersFile after applying -C.  Make both
    # paths absolute while the caller's working directory still defines them.
    repo = repo.resolve()
    anchor = anchor.resolve()
    if not repo.is_dir():
        return False, f"candidate repository does not exist: {repo}"
    if not anchor.is_file():
        return False, f"trusted allowed_signers file is missing: {anchor}"
    if not _git_version_ok():
        return False, "git 2.34+ is required to verify SSH commit signatures"
    target, error = _resolve_commit(repo, commit)
    if target is None:
        return False, f"could not resolve candidate commit: {error}"
    result = _run(
        [
            "git",
            "-c",
            "gpg.format=ssh",
            "-c",
            f"gpg.ssh.allowedSignersFile={anchor}",
            "-C",
            str(repo),
            "verify-commit",
            target,
        ]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"commit {target[:7]} failed signature verification: {detail}"
    return True, f"commit {target[:7]} signature verified with trusted base anchor"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a candidate commit with a trusted SSH anchor.")
    parser.add_argument("--repo", required=True, type=Path, help="repository containing the candidate object")
    parser.add_argument("--commit", required=True, help="exact candidate commit SHA")
    parser.add_argument("--anchor", required=True, type=Path, help="allowed_signers from the trusted base")
    args = parser.parse_args()

    ok, message = verify(args.repo, args.commit, args.anchor)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
