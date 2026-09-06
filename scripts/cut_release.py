#!/usr/bin/env python3
"""Cut an OMM stable release: fast-forward ``main`` to a vetted ``beta`` commit
and push a signed ``v<version>`` tag.

Since the 2026-09-06 branch redesign, ``beta`` is the trunk and ``main`` is a
stable release pointer that only ever *fast-forwards* to a commit that already
exists on ``beta``. That keeps ``main`` an ancestor of ``beta`` by construction,
which is what ``omm update``'s signature chain relies on
(``src/omm/trust/__init__.py``). This script is the only supported way to move
``main``.

It does not build or publish anything. Pushing the ``v<version>`` tag into
``main`` is the release trigger; ``release.yml`` / ``npm-release.yml`` /
``windows-portable.yml`` take over from there.

Usage:

    python scripts/cut_release.py                 # release origin/beta tip
    python scripts/cut_release.py --sha <commit>  # release a specific beta commit
    python scripts/cut_release.py --dry-run       # print the plan, touch nothing
    python scripts/cut_release.py --yes           # skip the confirmation prompt

The tag is signed with your local git signing config (the same setup that signs
your commits); this script does not manage keys.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseCutError(RuntimeError):
    """Raised when the release cut is unsafe and must not proceed."""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _out(*args: str) -> str:
    return _git(*args).stdout.strip()


def _is_ancestor(maybe_ancestor: str, descendant: str) -> bool:
    return (
        _git("merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False).returncode
        == 0
    )


def _require_clean_tree() -> None:
    if _out("status", "--porcelain"):
        raise ReleaseCutError(
            "working tree is not clean; commit or stash before cutting a release"
        )


def _version_in_text(text: str) -> str:
    table = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if table is None:
        raise ReleaseCutError("pyproject.toml has no [project] table")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', table.group(1))
    if match is None:
        raise ReleaseCutError("[project].version is not a literal string")
    return match.group(1)


def _version_at(commit: str) -> str:
    return _version_in_text(_out("show", f"{commit}:pyproject.toml"))


def plan_release(sha: str | None, remote: str) -> tuple[str, str]:
    """Validate the cut and return ``(target_commit, version)``. Never writes."""
    _require_clean_tree()

    _git("fetch", "--no-tags", remote, "beta", "main")
    beta = _out("rev-parse", f"{remote}/beta")
    main = _out("rev-parse", f"{remote}/main")
    target = _out("rev-parse", f"{sha}^{{commit}}") if sha else beta

    if not _is_ancestor(target, beta):
        raise ReleaseCutError(
            f"{target[:12]} is not contained in {remote}/beta - only a commit that "
            "has already landed on the trunk can be released"
        )
    if target == main:
        raise ReleaseCutError(f"{remote}/main is already at {target[:12]}; nothing to release")
    if not _is_ancestor(main, target):
        raise ReleaseCutError(
            f"{target[:12]} is not a fast-forward over {remote}/main - main must never "
            "be rewritten (it would strand every omm update client)"
        )

    version = _version_at(target)
    tag = f"v{version}"
    existing = _git("ls-remote", "--tags", remote, tag, check=False).stdout.strip()
    if existing:
        raise ReleaseCutError(f"tag {tag} already exists on {remote}: {existing.splitlines()[0]}")
    if _git("tag", "--list", tag).stdout.strip():
        raise ReleaseCutError(f"tag {tag} already exists locally")

    return target, version


def cut_release(sha: str | None, remote: str, *, dry_run: bool, assume_yes: bool) -> int:
    target, version = plan_release(sha, remote)
    tag = f"v{version}"
    ahead = _out("rev-list", "--count", f"{remote}/main..{target}")

    print(f"Release cut plan ({remote}):")
    print(f"  fast-forward main  {_out('rev-parse', '--short', f'{remote}/main')}"
          f" -> {target[:12]}  (+{ahead} commits from beta)")
    print(f"  sign + push tag    {tag}  on {target[:12]}")
    print(f"  trigger            release.yml / npm-release.yml / windows-portable.yml")

    if dry_run:
        print("\n--dry-run: nothing pushed.")
        return 0

    if not assume_yes:
        reply = input(f"\nPush {tag} and move {remote}/main? This starts a real release [y/N] ")
        if reply.strip().lower() not in {"y", "yes"}:
            print("aborted.")
            return 1

    # Fast-forward main. No --force: a non-fast-forward is rejected here and by
    # the remote, so a stale local view cannot rewrite main.
    _git("push", remote, f"{target}:refs/heads/main")
    # Signed annotated tag on the exact released commit, using the caller's
    # local signing config.
    _git("tag", "-s", tag, target, "-m", f"omm {tag}")
    _git("push", remote, tag)
    print(f"\nReleased {tag} at {target[:12]}. Watch the release workflows on {remote}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", help="beta commit to release (default: the origin/beta tip)")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    try:
        return cut_release(args.sha, args.remote, dry_run=args.dry_run, assume_yes=args.yes)
    except (ReleaseCutError, subprocess.SubprocessError, OSError) as error:
        print(f"release cut failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
