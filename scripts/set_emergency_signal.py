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
import os
import re
import tempfile
from pathlib import Path


_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _load_artifact(path: Path) -> dict:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict):
        raise ValueError("catalog artifact must contain a JSON object")
    return artifact


def _write_artifact(path: Path, artifact: dict) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(artifact, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_signal(artifact_path: Path, *, id_: str, message: str, fixed_in_version: str | None) -> None:
    id_ = id_.strip()
    message = message.strip()
    if not id_ or len(id_) > 128 or any(ord(character) < 32 for character in id_):
        raise ValueError("emergency signal id must be 1-128 printable characters")
    if not message or len(message) > 2_000:
        raise ValueError("emergency signal message must be 1-2000 characters")
    if fixed_in_version is not None and _VERSION_PATTERN.fullmatch(fixed_in_version) is None:
        raise ValueError("fixed_in_version must use X.Y.Z format")
    artifact = _load_artifact(artifact_path)
    signal = {"id": id_, "message": message}
    if fixed_in_version:
        signal["fixed_in_version"] = fixed_in_version
    artifact["emergency"] = signal
    _write_artifact(artifact_path, artifact)


def clear_signal(artifact_path: Path) -> None:
    artifact = _load_artifact(artifact_path)
    if "emergency" not in artifact:
        print("No emergency field present - nothing to clear.")
        return
    del artifact["emergency"]
    _write_artifact(artifact_path, artifact)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Path to localfit-recommend-model.json")
    parser.add_argument("--clear", action="store_true", help="Remove the emergency field instead of setting one")
    parser.add_argument("--id", dest="id_", help="Stable identifier for this signal")
    parser.add_argument("--message", help="User-facing message shown by omm")
    parser.add_argument("--fixed-in-version", dest="fixed_in_version", default=None)
    args = parser.parse_args()

    try:
        if args.clear:
            clear_signal(args.artifact)
            return

        if not args.id_ or not args.message or not args.message.strip():
            parser.error("--id and a non-empty --message are required unless --clear is passed")
        set_signal(
            args.artifact,
            id_=args.id_,
            message=args.message,
            fixed_in_version=args.fixed_in_version,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
