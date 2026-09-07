#!/usr/bin/env python3
"""Cut an OMM stable release: bump the patch version, fast-forward ``main`` (and
``beta``) to the release commit, and push a signed ``v<version>`` tag.

Since the 2026-09-06 branch redesign, ``beta`` is the trunk and ``main`` is a
stable release pointer that only ever *fast-forwards* to a commit that already
exists on ``beta``. That keeps ``main`` an ancestor of ``beta`` by construction,
which is what ``omm update``'s signature chain relies on
(``src/omm/trust/__init__.py``). This script is the only supported way to move
``main`` and the only thing that changes the version number: between releases
every ``beta`` commit carries the last release's ``X.Y.Z`` (``omm --version``
disambiguates by commit hash).

Each cut bumps the patch by exactly one (``0.3.52`` -> ``0.3.53``). It does not
build or publish; pushing the ``v<version>`` tag is the release trigger, and
``release.yml`` / ``npm-release.yml`` / ``windows-portable.yml`` take over (each
PyPI/npm publish still pauses on its deploy-environment reviewers).

Usage:

    python scripts/cut_release.py                # release the origin/beta tip
    python scripts/cut_release.py --dry-run      # show the promotion, touch nothing
    python scripts/cut_release.py --yes          # skip the confirmation prompt
    python scripts/cut_release.py --skip-ci-check   # before beta protection exists

The release commit and tag are signed with your local git signing config; this
script does not manage keys. Pushing the ``release:`` commit straight to ``beta``
needs your account in the branch's bypass-pull-request allowance.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NPM_LAUNCHER = ROOT / "packaging" / "npm" / "launcher" / "package.json"


class ReleaseCutError(RuntimeError):
    """Raised when the release cut is unsafe and must not proceed."""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
    )


def _out(*args: str) -> str:
    return _git(*args).stdout.strip()


def _gh(*args: str) -> str:
    """`gh api` call. Split out so tests can stub the GitHub side."""
    return subprocess.run(
        ["gh", *args],
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
    ).stdout


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


def _repo_slug(remote: str) -> str:
    url = _out("remote", "get-url", remote)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if match is None:
        raise ReleaseCutError(f"cannot parse a github owner/repo from {remote} url {url!r}")
    return match.group(1)


def _version_in_text(text: str) -> str:
    table = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if table is None:
        raise ReleaseCutError("pyproject.toml has no [project] table")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', table.group(1))
    if match is None:
        raise ReleaseCutError("[project].version is not a literal string")
    return match.group(1)


def _bump_patch(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ReleaseCutError(
            f"pyproject version {version!r} is not a plain X.Y.Z - cut_release only "
            "bumps the patch of a release number"
        )
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _required_contexts(slug: str) -> list[str]:
    payload = json.loads(_gh("api", f"repos/{slug}/branches/beta/protection"))
    return list(payload.get("required_status_checks", {}).get("contexts", []))


def _failing_required_checks(slug: str, sha: str) -> list[str]:
    required = _required_contexts(slug)
    if not required:
        raise ReleaseCutError(
            "beta has no required status checks configured - cannot verify the "
            "release target is green (pass --skip-ci-check to override)"
        )
    payload = json.loads(_gh("api", f"repos/{slug}/commits/{sha}/check-runs", "--paginate"))
    conclusions: dict[str, str] = {}
    for run in payload.get("check_runs", []):
        conclusions[run.get("name", "")] = run.get("conclusion") or "pending"
    return [name for name in required if conclusions.get(name) != "success"]


def _set_version_files(version: str) -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    PYPROJECT.write_text(
        re.sub(r'(?m)^(version\s*=\s*)"[^"]+"', rf'\1"{version}"', pyproject, count=1),
        encoding="utf-8",
    )
    if NPM_LAUNCHER.is_file():
        launcher = NPM_LAUNCHER.read_text(encoding="utf-8")
        launcher = re.sub(r'"version": "[0-9]+\.[0-9]+\.[0-9]+"', f'"version": "{version}"', launcher)
        launcher = re.sub(
            r'("@omm-hippo/omm-[a-z0-9-]+": ")[0-9]+\.[0-9]+\.[0-9]+"',
            rf'\g<1>{version}"',
            launcher,
        )
        NPM_LAUNCHER.write_text(launcher, encoding="utf-8")


def plan_release(remote: str, *, skip_ci_check: bool) -> tuple[str, str, str]:
    """Validate the cut. Returns ``(target_commit, current_version, next_version)``.
    Never writes."""
    _require_clean_tree()
    _git("fetch", "--no-tags", remote, "beta", "main")
    beta = _out("rev-parse", f"{remote}/beta")
    main = _out("rev-parse", f"{remote}/main")

    if beta == main:
        raise ReleaseCutError(f"{remote}/main is already at the {remote}/beta tip; nothing to release")
    if not _is_ancestor(main, beta):
        raise ReleaseCutError(
            f"{remote}/beta is not a fast-forward over {remote}/main - the branches "
            "diverged, which must never happen (fix before releasing)"
        )

    current = _version_in_text(_out("show", f"{beta}:pyproject.toml"))
    nxt = _bump_patch(current)
    tag = f"v{nxt}"
    if _git("ls-remote", "--tags", remote, tag, check=False).stdout.strip():
        raise ReleaseCutError(f"tag {tag} already exists on {remote}")
    if _git("tag", "--list", tag).stdout.strip():
        raise ReleaseCutError(f"tag {tag} already exists locally")

    if not skip_ci_check:
        failing = _failing_required_checks(_repo_slug(remote), beta)
        if failing:
            raise ReleaseCutError(
                f"required checks not green on {beta[:12]}: {', '.join(failing)}"
            )

    return beta, current, nxt


def _print_promotion(remote: str, target: str) -> int:
    base = f"{remote}/main"
    count = int(_out("rev-list", "--count", f"{base}..{target}"))
    print(f"\nPromoting {count} commit(s) {base} -> {target[:12]}:\n")
    print(_out("log", "--oneline", "--no-merges", f"{base}..{target}") or "  (merges only)")
    print("\nFiles changed:")
    print(_out("diff", "--stat", f"{base}..{target}"))
    return count


def cut_release(remote: str, *, dry_run: bool, assume_yes: bool, skip_ci_check: bool) -> int:
    target, current, nxt = plan_release(remote, skip_ci_check=skip_ci_check)
    tag = f"v{nxt}"

    count = _print_promotion(remote, target)
    print(f"\nRelease: {current} -> {nxt}   tag {tag} on the new release commit")
    print("Trigger: release.yml / npm-release.yml / windows-portable.yml "
          "(publish still waits on deploy-environment reviewers)")

    if dry_run:
        print("\n--dry-run: nothing written or pushed.")
        return 0
    if not assume_yes:
        reply = input(f"\nPromote {count} commit(s) and release {tag}? [y/N] ")
        if reply.strip().lower() not in {"y", "yes"}:
            print("aborted.")
            return 1

    # Build the release commit on top of the target.
    _git("checkout", "--quiet", "--detach", target)
    try:
        _set_version_files(nxt)
        _git("add", "pyproject.toml", "packaging/npm/launcher/package.json")
        _git("commit", "--quiet", "-m", f"release: {tag}")
        release_commit = _out("rev-parse", "HEAD")
    finally:
        # Leave the caller's checkout as they left it regardless of outcome.
        _git("checkout", "--quiet", "-")

    # Fast-forward beta then main to the release commit (no --force: a non-FF is
    # rejected locally and by the remote). beta needs the caller in the branch's
    # bypass-pull-request allowance.
    _git("push", remote, f"{release_commit}:refs/heads/beta")
    _git("push", remote, f"{release_commit}:refs/heads/main")
    _git("tag", "-s", tag, release_commit, "-m", f"omm {tag}")
    _git("push", remote, tag)
    print(f"\nReleased {tag} at {release_commit[:12]}. Watch the release workflows on {remote}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--dry-run", action="store_true", help="show the promotion and exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--skip-ci-check",
        action="store_true",
        help="do not verify the target's required checks (only before beta protection exists)",
    )
    args = parser.parse_args()
    try:
        return cut_release(
            args.remote,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            skip_ci_check=args.skip_ci_check,
        )
    except (ReleaseCutError, subprocess.SubprocessError, OSError) as error:
        print(f"release cut failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
