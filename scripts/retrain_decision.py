#!/usr/bin/env python3
"""Validate a nightly retrain result and describe the publication action."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


_PUBLICATION_METADATA = {
    "passed": {
        "commit_message": "chore: retrain recommendation model",
        "pr_title": "chore: nightly recommendation model retrain",
        "pr_body": (
            "Automated nightly retrain. Candidate passed the quality gate against "
            "the current published baseline; see the workflow run for the full "
            "quality report."
        ),
    },
    "rejected": {
        "commit_message": "chore: refresh candidates after rejected retrain",
        "pr_title": "chore: refresh candidate pool after rejected retrain",
        "pr_body": (
            "Automated nightly run. The trained candidate did not pass the quality "
            "gate, so the published recommendation model and its signature remain "
            "unchanged. This PR contains only independent candidate-pool updates; "
            "see the workflow run for the rejection details."
        ),
    },
    "skipped": {
        "commit_message": "chore: refresh candidates after skipped retrain",
        "pr_title": "chore: refresh candidate pool after skipped retrain",
        "pr_body": (
            "Automated nightly run. Model publication was skipped because the quality "
            "gate lacked sufficient evidence, so the published recommendation model "
            "and its signature remain unchanged. This PR contains only independent "
            "candidate-pool updates; see the workflow run for details."
        ),
    },
}


def quality_gate_status(report: object) -> str:
    if not isinstance(report, dict):
        raise ValueError("quality report must be a JSON object")
    passed = report.get("passed")
    skipped = report.get("skipped", False)
    if not isinstance(passed, bool):
        raise ValueError("quality report passed must be a boolean")
    if not isinstance(skipped, bool):
        raise ValueError("quality report skipped must be a boolean")
    if passed and skipped:
        raise ValueError("quality report cannot be both passed and skipped")
    if skipped:
        return "skipped"
    return "passed" if passed else "rejected"


def publication_outputs(
    report: object,
    candidate_content: bytes,
    incumbent_content: bytes,
) -> dict[str, str]:
    status = quality_gate_status(report)
    model_changed = candidate_content != incumbent_content
    if status != "passed" and model_changed:
        raise ValueError(
            f"quality gate reported {status} but changed the published model"
        )
    return {
        "quality_gate_status": status,
        "model_changed": str(model_changed).lower(),
        **_PUBLICATION_METADATA[status],
    }


def inspect_files(
    quality_report: Path,
    candidate: Path,
    incumbent: Path,
) -> dict[str, str]:
    try:
        report = json.loads(quality_report.read_text(encoding="utf-8"))
        candidate_content = candidate.read_bytes()
        incumbent_content = incumbent.read_bytes()
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"could not inspect retrain result: {error}") from error
    return publication_outputs(report, candidate_content, incumbent_content)


def append_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    for key, value in outputs.items():
        if "\n" in key or "\n" in value:
            raise ValueError("GitHub step outputs must be single-line values")
    try:
        with path.open("a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    except OSError as error:
        raise ValueError(f"could not write GitHub step outputs: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--incumbent", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        outputs = inspect_files(args.quality_report, args.candidate, args.incumbent)
        append_github_outputs(args.github_output, outputs)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
