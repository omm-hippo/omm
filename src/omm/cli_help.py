"""Short usage errors and best-effort hints scoped to the current terminal."""
from __future__ import annotations

import json
import sys

try:
    from typer._click.exceptions import UsageError
except ImportError:
    from click.exceptions import UsageError

from omm import session_cache
from omm.atomic import atomic_write_text, locked
from omm.json_output import write_document


def option_requested(args: list[str], option: str) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg == option:
            return True
    return False


def engine_read_only_args(args: list[str]) -> bool:
    before_separator = args[:args.index("--")] if "--" in args else args
    commands = [token for token in before_separator if not token.startswith("-")]
    return len(commands) >= 2 and commands[:2] in (["engine", "status"], ["engine", "doctor"])


def first_terminal_hint(command: str) -> bool:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    session = session_cache._session_path()
    if session is None:
        return False
    path = session.with_name(session.stem + ".help.json")
    try:
        with locked(path, timeout=0):
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size <= 8192 else []
            except (OSError, ValueError):
                data = []
            seen = [item for item in data if isinstance(item, str)] if isinstance(data, list) else []
            if command in seen:
                return False
            atomic_write_text(path, json.dumps((seen + [command])[-64:]))
            return True
    except OSError:
        return False


class BriefUsageError(UsageError):
    def __init__(self, original: UsageError, root_ctx):
        super().__init__(original.format_message(), ctx=original.ctx or root_ctx)
        self.json_requested = bool(root_ctx.meta.get("omm_json_requested"))
        self.read_only = bool(root_ctx.meta.get("omm_engine_read_only"))

    def show(self, file=None) -> None:
        command = self.ctx.command_path if self.ctx is not None else "omm"
        if self.json_requested:
            write_document({"schema_version": 1, "status": "error", "error": {
                "code": "invalid_arguments", "command": command,
                "message": self.message, "exit_code": self.exit_code,
            }})
            return
        output = file or sys.stderr
        output.write(f"Error: {self.message}\n")
        if not self.read_only and first_terminal_hint(command) and self.ctx is not None:
            short_help = self.ctx.command.get_short_help_str(limit=120)
            if short_help:
                output.write(short_help + "\n")
            output.write(self.ctx.get_usage() + "\n")
        output.write(f"Run '{command} --help' for options and examples.\n")
