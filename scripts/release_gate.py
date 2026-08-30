#!/usr/bin/env python3
"""Decide whether a merged PR carries enough approving reviews to auto-release.

The GitHub Release workflow runs this after a signed ``v*`` tag is pushed.  It
feeds the merged pull request's review list in and expects a non-zero exit when
fewer than ``--min`` distinct teammates (excluding the author) currently
approve.  ``COMMENTED`` and ``PENDING`` reviews never change a reviewer's
standing, so only the most recent ``APPROVED`` / ``CHANGES_REQUESTED`` /
``DISMISSED`` review per person counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_STANDING_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def flatten_pages(payload: object) -> list:
    """Accept a flat review array or ``gh api --paginate --slurp``'s array of pages."""
    if isinstance(payload, list) and payload and all(
        isinstance(page, list) for page in payload
    ):
        return [review for page in payload for review in page]
    return payload


def latest_standing_by_user(reviews: object) -> dict[str, str]:
    """Map each reviewer login to its last review state that affects standing."""
    if not isinstance(reviews, list):
        raise ValueError("reviews payload must be a JSON array")
    standing: dict[str, str] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("each review must be a JSON object")
        state = review.get("state")
        user = review.get("user") or {}
        login = user.get("login")
        if not isinstance(login, str):
            # Reviews from a since-deleted account carry a null user and cannot
            # be attributed to a teammate, so they never count toward approval.
            continue
        if not isinstance(state, str):
            raise ValueError("each review needs a string state")
        if state in _STANDING_STATES:
            standing[login] = state
    return standing


def approving_reviewers(reviews: object, author: str) -> list[str]:
    standing = latest_standing_by_user(reviews)
    return sorted(
        login
        for login, state in standing.items()
        if state == "APPROVED" and login != author
    )


def evaluate(reviews: object, author: str, minimum: int) -> dict[str, str]:
    approvers = approving_reviewers(reviews, author)
    return {
        "approved": "true" if len(approvers) >= minimum else "false",
        "approver_count": str(len(approvers)),
        "approvers": ",".join(approvers),
    }


def append_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            stream.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reviews",
        type=Path,
        required=True,
        help="file holding the PR reviews JSON array ('-' for stdin)",
    )
    parser.add_argument(
        "--author", required=True, help="PR author login, never counted as an approver"
    )
    parser.add_argument("--min", type=int, default=2, dest="minimum")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    if str(args.reviews) == "-":
        raw = sys.stdin.read()
    else:
        raw = args.reviews.read_text(encoding="utf-8")
    try:
        outputs = evaluate(flatten_pages(json.loads(raw)), args.author, args.minimum)
    except ValueError as error:
        parser.error(str(error))

    if args.github_output:
        append_github_outputs(args.github_output, outputs)

    if outputs["approved"] == "true":
        print(
            f"{outputs['approver_count']} approving reviewer(s): {outputs['approvers']}"
        )
        return
    raise SystemExit(
        f"only {outputs['approver_count']} approving reviewer(s) "
        f"({outputs['approvers'] or 'none'}); need {args.minimum}. "
        "Create the release manually or gather more approvals."
    )


if __name__ == "__main__":
    main()
