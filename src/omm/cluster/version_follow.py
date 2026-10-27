"""A cluster member updating itself to exactly the head's omm commit.

The head only says which commit; the member fetches it from GitHub and
installs it through `omm update`'s own trust path, so a commit that is not
signed, or not on the channel branch, is never installed. A member never
moves backward: when it is ahead, the fix is `omm update` on the head.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable

# What a head may send. The commit reaches git's argv, so nothing else gets through.
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_BRANCHES = ("main", "beta")


def local_version() -> dict:
    from omm import cli, package_metadata

    return {"commit": cli._src_head_commit(), "branch": cli._channel_branch(),
            "git": package_metadata.install_source() is package_metadata.InstallSource.GIT}


def _is_ancestor(ancestor: str, descendant: str) -> bool | None:
    from omm import cli

    result = cli._run_git(["git", "-C", str(cli.SRC_DIR), "merge-base", "--is-ancestor", ancestor, descendant],
                          timeout=10)
    return {0: True, 1: False}.get(result.returncode)


def decide(local: dict, head: dict, *, is_ancestor: Callable[[str, str], bool | None]) -> str:
    """'same', 'update', 'switch' (other channel), 'ahead' or 'manual' (not a git install on one side)."""
    mine, theirs = local.get("commit"), head.get("commit")
    if not (local.get("git") and head.get("git") and mine and theirs):
        return "manual"
    if head.get("branch") != local.get("branch"):
        return "switch"
    if mine == theirs:
        return "same"
    if is_ancestor(theirs, mine):
        return "ahead"
    return "update"


def apply(head: dict) -> tuple[str, str | None]:
    """Follow the head; returns (state, error) with state same|updated|switched|ahead|manual|blocked."""
    from omm import cli, config

    if head.get("git") and head.get("commit") is not None and not (
            isinstance(head["commit"], str) and _COMMIT_RE.fullmatch(head["commit"]) and head.get("branch") in _BRANCHES):
        return "blocked", "the head sent an invalid version"
    action = decide(local_version(), head, is_ancestor=_is_ancestor)
    if action == "same" and not cli._deps_satisfied():
        # An earlier follow checked out the code but could not install its new
        # dependencies; this omm would fail to start next time.
        action = "update"
    if action in ("same", "ahead", "manual"):
        return action, None
    branch, commit = head["branch"], head["commit"]
    try:
        if action == "switch":
            result: subprocess.CompletedProcess = cli._perform_update(branch, same_branch=False, target_commit=commit)
        else:
            result = cli._perform_update(branch, target_commit=commit)
    except (Exception, SystemExit) as error:
        return "blocked", f"omm update failed: {error!r}"
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        return "blocked", lines[0] if lines else "omm update failed"
    if action == "switch":
        config.update_config(update_channel="beta" if branch == "beta" else "stable")
        return "switched", None
    return "updated", None
