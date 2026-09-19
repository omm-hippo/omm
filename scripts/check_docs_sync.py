#!/usr/bin/env python3
"""Fail when the three command-documentation surfaces drift apart.

The omm CLI (`src/omm/cli.py`) is the single source of truth. This script
checks that:

1. `docs/commands.json` still matches what the CLI would generate.
2. `README.md`'s `## Usage` section mentions every command, invents no
   command that does not exist, and passes no flag a command does not have.
3. The omm.run copy of `commands.json` matches ours. A mismatch only prints
   a GitHub warning: the site lives in another repository, so failing here
   would deadlock both pull requests.

    python scripts/check_docs_sync.py

Exit status is 1 when check 1 or 2 fails, 0 otherwise.

`omm_version` is deliberately excluded from every comparison: `scripts/pre-commit`
bumps the patch version on every single commit, which would make the generated
file permanently "stale" for reasons that have nothing to do with the commands.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JSON = REPO_ROOT / "docs" / "commands.json"
README = REPO_ROOT / "README.md"
REMOTE_COMMANDS_URL = (
    "https://raw.githubusercontent.com/omm-hippo/omm.run/main/src/data/commands.json"
)
REMOTE_TIMEOUT_SECONDS = 10

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

# `omm <word>` in a README usage line. The first token decides the command.
_OMM_CALL_RE = re.compile(r"(?<![\w./-])omm\s+")
_TRAILING_COMMENT_RE = re.compile(r"\s+#\s.*$")
_WORD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


# --------------------------------------------------------------------------
# 1. docs/commands.json freshness
# --------------------------------------------------------------------------


def _comparable(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "omm_version"}


def build_current_reference() -> dict[str, Any]:
    from omm.cli import app
    from omm.command_reference import build_reference

    return build_reference(app)


def check_commands_json(current: dict[str, Any], problems: list[str]) -> dict[str, Any] | None:
    if not COMMANDS_JSON.is_file():
        _fail(
            problems,
            "docs/commands.json is missing - run `python scripts/export_command_reference.py`",
        )
        return None
    committed = json.loads(COMMANDS_JSON.read_text(encoding="utf-8"))
    if _comparable(committed) == _comparable(current):
        return committed

    current_paths = {tuple(entry["path"]) for entry in current["commands"]}
    committed_paths = {tuple(entry["path"]) for entry in committed.get("commands", [])}
    for path in sorted(current_paths - committed_paths):
        _fail(problems, f"docs/commands.json is missing command `omm {' '.join(path)}`")
    for path in sorted(committed_paths - current_paths):
        _fail(problems, f"docs/commands.json still lists removed command `omm {' '.join(path)}`")
    committed_by_path = {tuple(e["path"]): e for e in committed.get("commands", [])}
    for entry in current["commands"]:
        path = tuple(entry["path"])
        other = committed_by_path.get(path)
        if other is not None and other != entry:
            changed = sorted(k for k in entry if entry[k] != other.get(k))
            _fail(
                problems,
                f"docs/commands.json is out of date for `omm {' '.join(path)}`"
                f" (changed: {', '.join(changed)})",
            )
    _fail(problems, "docs/commands.json is stale - run `python scripts/export_command_reference.py`")
    return committed


# --------------------------------------------------------------------------
# 2. README ## Usage section
# --------------------------------------------------------------------------


def usage_section(text: str) -> str:
    match = re.search(r"^## Usage$", text, re.MULTILINE)
    if match is None:
        raise SystemExit("README.md has no `## Usage` section.")
    rest = text[match.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _known_paths(reference: dict[str, Any]) -> tuple[set[str], dict[str, set[str]]]:
    """(top-level names incl. aliases, {group name: subcommand names})."""
    top: set[str] = set()
    subs: dict[str, set[str]] = {}
    for entry in reference["commands"]:
        path = entry["path"]
        if len(path) == 1:
            top.add(path[0])
            top.update(entry["aliases"])
            if entry["kind"] == "group":
                subs.setdefault(path[0], set())
        elif len(path) == 2:
            subs.setdefault(path[0], set()).add(path[1])
    return top, subs


def _entry_for(reference: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    for entry in reference["commands"]:
        if tuple(entry["path"]) == path:
            return entry
    return None


def _omm_calls(line: str) -> list[str]:
    """Each `omm ...` invocation on one README line, comment stripped."""
    text = _TRAILING_COMMENT_RE.sub("", line.strip())
    matches = list(_OMM_CALL_RE.finditer(text))
    ends = [match.start() for match in matches][1:] + [len(text)]
    return [text[match.end() : end] for match, end in zip(matches, ends)]


def _global_flags(reference: dict[str, Any]) -> set[str]:
    return {
        flag
        for entry in reference["commands"]
        for option in entry["options"]
        if option["global"]
        for flag in option["flags"]
    }


def check_readme(reference: dict[str, Any], problems: list[str]) -> None:
    section = usage_section(README.read_text(encoding="utf-8"))
    code = "\n".join(_CODE_FENCE_RE.findall(section))
    top, subs = _known_paths(reference)
    global_flags = _global_flags(reference)

    # (a) every documented command is mentioned somewhere in ## Usage.
    for entry in reference["commands"]:
        path = entry["path"]
        if len(path) == 2 and path[0] not in ("engine", "setting"):
            continue
        mention = "omm " + " ".join(path)
        if mention not in section:
            _fail(problems, f"README.md `## Usage` never mentions `{mention}`")

    # (b)/(c) every `omm ...` line in the Usage code blocks is real, and only
    # uses flags the command actually has.
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for call in _omm_calls(stripped):
            tokens = call.split()
            if not tokens or not _WORD_RE.match(tokens[0]):
                continue
            name = tokens[0]
            if name not in top:
                _fail(
                    problems,
                    f"README.md `## Usage` uses `omm {name}`, which is not a command"
                    f" (line: {stripped})",
                )
                continue
            path = [name]
            if name in subs and len(tokens) > 1 and _WORD_RE.match(tokens[1]):
                if tokens[1] not in subs[name]:
                    _fail(
                        problems,
                        f"README.md `## Usage` uses `omm {name} {tokens[1]}`,"
                        f" which is not a subcommand of `omm {name}` (line: {stripped})",
                    )
                    continue
                path.append(tokens[1])
            entry = _entry_for(reference, tuple(path))
            # Aliases (`omm ls`) and groups whose own subcommands carry the
            # flags (`omm setting upload crash --enable`) are not checkable
            # here; the subcommand's own line is.
            if entry is None or entry["kind"] == "group":
                continue
            allowed = {
                flag for option in entry["options"] for flag in option["flags"]
            } | global_flags | {"--help"}
            for flag in _FLAG_RE.findall(call):
                if flag not in allowed:
                    _fail(
                        problems,
                        f"README.md `## Usage` passes `{flag}` to `omm {' '.join(path)}`,"
                        f" which has no such flag (line: {stripped})",
                    )


# --------------------------------------------------------------------------
# 3. omm.run copy (warning only)
# --------------------------------------------------------------------------


def check_site_copy(local: dict[str, Any]) -> None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(REMOTE_COMMANDS_URL, timeout=REMOTE_TIMEOUT_SECONDS) as response:
            remote = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print(f"note: skipped the omm.run comparison ({type(error).__name__}: {error}).")
        return
    if _comparable(remote) == _comparable(local):
        print("omm.run copy of commands.json is up to date.")
        return
    print(
        "::warning::omm.run copy is stale - run `npm run sync-commands` in omm.run"
    )


def main() -> int:
    problems: list[str] = []
    current = build_current_reference()
    committed = check_commands_json(current, problems)
    check_readme(current, problems)
    check_site_copy(committed or current)

    if problems:
        print(f"\ndocs-sync found {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"docs-sync OK ({len(current['commands'])} commands).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
