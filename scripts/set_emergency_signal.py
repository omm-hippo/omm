#!/usr/bin/env python3
"""Add, replace, or clear the `emergency` field on published/localfit-recommend-model.json.

This is the human-triggered "break glass" half of the emergency-update
signal (see src/omm/predictor.py's extract_emergency_signal and
src/omm/cli.py's _handle_emergency_signal): when set, any locally-installed
omm older than fixed_in_version blocks on `recommend`/`search`/`contribute`
until the user updates.

Run via the "Emergency update signal" GitHub Actions workflow
(workflow_dispatch), never by hand against a signing key on a developer
machine - see that workflow for how the result gets signed and merged.
This script only edits the JSON; it does not sign or publish anything
itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def set_signal(artifact_path: Path, *, id_: str, message: str, fixed_in_version: str | None) -> None:
    artifact = json.loads(artifact_path.read_text())
    signal = {"id": id_, "message": message}
    if fixed_in_version:
        signal["fixed_in_version"] = fixed_in_version
    artifact["emergency"] = signal
    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n")


def clear_signal(artifact_path: Path) -> None:
    artifact = json.loads(artifact_path.read_text())
    if "emergency" not in artifact:
        print("No emergency field present - nothing to clear.")
        return
    del artifact["emergency"]
    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Path to localfit-recommend-model.json")
    parser.add_argument("--clear", action="store_true", help="Remove the emergency field instead of setting one")
    parser.add_argument("--id", dest="id_", help="Stable identifier for this signal")
    parser.add_argument("--message", help="User-facing message shown by omm")
    parser.add_argument("--fixed-in-version", dest="fixed_in_version", default=None)
    args = parser.parse_args()

    if args.clear:
        clear_signal(args.artifact)
        return

    if not args.id_ or not args.message or not args.message.strip():
        parser.error("--id and a non-empty --message are required unless --clear is passed")
    set_signal(args.artifact, id_=args.id_, message=args.message, fixed_in_version=args.fixed_in_version)


if __name__ == "__main__":
    main()
