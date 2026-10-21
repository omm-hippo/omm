"""Bounded local stage counts, without copying log messages or identifiers."""

from __future__ import annotations

import json

from omm import config

MAX_FILES = 50
MAX_FILE_BYTES = 1024 * 1024
MAX_LINES = 2000
COMMAND_STAGES = {
    "setup": "setup", "install": "install", "upgrade": "install",
    "search": "recommend", "recommend": "recommend",
    "link": "link", "relink": "link",
}
NEXT_STEPS = {
    "setup": "Run omm doctor, then rerun omm setup after resolving its findings.",
    "recommend": "Check omm setting catalog-status; retry with a cached catalog in offline mode.",
    "install": "Run omm doctor and omm list before retrying the exact install reference.",
    "download": "Check available storage and network access, then retry the exact install reference.",
    "link": "Run omm engine doctor ENGINE, then retry omm link MODEL --engine ENGINE.",
}


def collect() -> dict:
    stages = {name: {"started": 0, "succeeded": 0, "failed": 0, "incomplete": 0,
                     "later_success_after_failure": 0} for name in NEXT_STEPS}
    unresolved = {name: False for name in stages}
    scanned = skipped = 0
    root = config.OMM_HOME / "logs"
    try:
        paths = sorted(root.glob("*.jsonl"))[-MAX_FILES:]
    except OSError:
        paths = []

    def outcome(stage, value):
        stages[stage][value] += 1
        if value == "failed":
            unresolved[stage] = True
        elif value == "succeeded" and unresolved[stage]:
            stages[stage]["later_success_after_failure"] += 1
            unresolved[stage] = False

    for path in paths:
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            with path.open(encoding="utf-8") as stream:
                records = []
                for index, line in enumerate(stream):
                    if index >= MAX_LINES:
                        skipped += 1
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except (OSError, UnicodeError):
            skipped += 1
            continue
        scanned += 1
        start = next((r for r in records if r.get("event") == "run_start"), {})
        argv = start.get("argv")
        command = next((x for x in argv if isinstance(x, str) and not x.startswith("-")), None) if isinstance(argv, list) else None
        stage = COMMAND_STAGES.get(command)
        if stage:
            stages[stage]["started"] += 1
            end = next((r for r in reversed(records) if r.get("event") == "run_end"), {})
            ending = end.get("outcome")
            value = {"ok": "succeeded", "failed": "failed"}.get(ending, "incomplete") if isinstance(ending, str) else "incomplete"
            outcome(stage, value)
        for record in records:
            event = record.get("event")
            if not isinstance(event, str):
                continue
            if event == "download-start":
                stages["download"]["started"] += 1
            elif event in {"download-complete", "download-failed"}:
                outcome("download", "succeeded" if event == "download-complete" else "failed")
            elif event == "link" and stage != "link":
                stages["link"]["started"] += 1
                outcome("link", "succeeded")
    stages["download"]["incomplete"] = max(0, stages["download"]["started"] - stages["download"]["succeeded"] - stages["download"]["failed"])
    return {
        "files_scanned": scanned, "files_skipped_or_truncated": skipped,
        "stages": [{"stage": name, **counts, "next_step": NEXT_STEPS[name] if counts["failed"] or counts["incomplete"] else None}
                   for name, counts in stages.items()],
        "limits": [
            "Only bounded recent local logs were inspected; missing logs are not evidence of success.",
            "A later success at the same stage does not establish recovery of the same model or request.",
            "Messages, model names, paths, arguments, URLs and credentials are excluded from this report.",
            "Stage summaries remain local and are not added to any upload channel.",
        ],
    }
