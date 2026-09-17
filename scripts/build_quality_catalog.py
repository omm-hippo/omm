#!/usr/bin/env python3
"""Build an unsigned official-quality artifact from verified local reports.

Sign the resulting JSON separately with scripts/sign_catalog.py. This script
does not promote community telemetry and refuses reports without exact package
identity or reports that claim to retain raw model responses.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from omm import quality_catalog


def evaluation_from_report(report: object) -> dict:
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("unsupported coding evaluation report")
    if report.get("raw_responses_stored") is not False:
        raise ValueError("official quality reports must not store raw responses")
    provider = report.get("model_provider")
    repo_id = report.get("model_repo_id")
    filename = report.get("model_filename")
    digest = report.get("model_digest")
    quantization = report.get("quantization")
    pack = report.get("pack")
    summary = report.get("summary")
    if not all(isinstance(value, str) and value for value in (provider, repo_id, filename, digest, quantization)):
        raise ValueError("official quality reports require exact package identity")
    if not isinstance(pack, dict) or not isinstance(summary, dict):
        raise ValueError("official quality report is missing pack or summary")
    score = summary.get("score")
    by_kind = summary.get("by_kind")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isinstance(by_kind, dict):
        raise ValueError("official quality report has invalid score data")
    parts = []
    for kind in ("generation", "repair"):
        values = by_kind.get(kind)
        if isinstance(values, dict):
            parts.append(f"{kind} {values.get('solved', 0)}/{values.get('total', 0)}")
    row = {
        "provider": provider,
        "repo_id": repo_id,
        "filename": filename,
        "model_digest": digest,
        "quantization": quantization,
        "task": "Coding",
        "pack_id": pack.get("id"),
        "pack_version": pack.get("version"),
        "pack_sha256": pack.get("sha256"),
        "score": score,
        "summary": "; ".join(parts) or f"{summary.get('solved', 0)}/{summary.get('total', 0)} tasks solved",
        "source": "official",
        "engine": report.get("engine"),
        "engine_version": report.get("engine_version"),
        "recorded_at": report.get("created_at"),
        "metrics": by_kind,
    }
    # Validate using the runtime consumer before the artifact is written.
    quality_catalog.build_index(
        {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "evaluations": [row]}
    )
    return row


def build(reports: list[Path]) -> dict:
    evaluations = []
    for path in reports:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f"could not read evaluation report {path}: {error}") from error
        evaluations.append(evaluation_from_report(report))
    artifact = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluations": sorted(
            evaluations,
            key=lambda row: (row["provider"], row["repo_id"], row["filename"], row["task"]),
        ),
    }
    quality_catalog.build_index(artifact)
    return artifact


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_atomic(args.output, build(args.reports))


if __name__ == "__main__":
    main()
