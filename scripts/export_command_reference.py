#!/usr/bin/env python3
"""Write `docs/commands.json` from the omm CLI.

The Typer app in `src/omm/cli.py` is the single source of truth for the
command surface. Run this after adding, renaming, or re-documenting any
command; `scripts/check_docs_sync.py` (CI job `docs-sync`) fails when the
committed file no longer matches the CLI.

    python scripts/export_command_reference.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "docs" / "commands.json"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def render() -> str:
    from omm.cli import app
    from omm.command_reference import build_reference

    # sort_keys stays off: the key order below is the documented schema
    # order the omm.run site renders against.
    return json.dumps(build_reference(app), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    text = render()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8", newline="\n")
    count = len(json.loads(text)["commands"])
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT).as_posix()} ({count} commands).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
