"""Machine-readable reference for every omm CLI command.

The Typer app in `omm.cli` is the single source of truth for what commands
exist, what they accept, and what they do. This module walks that app and
renders it as plain data so the three user-facing surfaces stay in sync:

1. `omm <command> --help`   - rendered by Click from the same objects.
2. `README.md` `## Usage`   - checked against this data by
   `scripts/check_docs_sync.py`.
3. https://omm.run/commands - rendered by the omm.run site from
   `docs/commands.json`, which `scripts/export_command_reference.py` writes
   from `build_reference()`.

The JSON shape is a contract with the omm.run repository; bump
`SCHEMA_VERSION` (and the site) when it changes incompatibly.
"""

from __future__ import annotations

import re
from typing import Any

import click
import typer
import typer.main

SCHEMA_VERSION = 1
GENERATED_BY = "scripts/export_command_reference.py"
DOCS_BASE_URL = "https://omm.run/commands"

# Top-level groups whose subcommands are part of the documented surface.
# Deeper groups (`setting upload`, `setting auto-import`) are listed as
# groups but their own children live under the parent group's page.
NESTED_GROUPS = ("engine", "setting")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# `Alias: ls` trailer inside a command docstring. The authoritative alias
# mapping is `cli._COMMAND_ALIASES`; this pattern only removes the prose
# duplicate from `description`.
_ALIAS_LINE_RE = re.compile(r"^[ \t]*Alias(?:es)?:[^\n]*$", re.MULTILINE)
_USAGE_PREFIX_RE = re.compile(r"^usage:\s*", re.IGNORECASE)


def docs_url(path: list[str] | tuple[str, ...]) -> str:
    """Public documentation URL for a command path.

    Top-level commands get their own page; a subcommand is an anchor on its
    group's page. Anything deeper than two levels is documented on the same
    anchor as its parent subgroup.
    """
    if not path:
        return DOCS_BASE_URL
    url = f"{DOCS_BASE_URL}/{path[0]}"
    if len(path) >= 2:
        url += f"#{path[1]}"
    return url


def docs_epilog(path: list[str] | tuple[str, ...]) -> str:
    """The one-line `--help` footer pointing at the web reference."""
    return f"More about omm {' '.join(path)}: {docs_url(path)}"


def _plain(text: str | None) -> str:
    return _ANSI_RE.sub("", text or "").strip()


def _split_help(help_text: str | None) -> tuple[str, str]:
    """Split a command's help into (summary, description).

    The summary is the first paragraph collapsed onto one line; the
    description is every later paragraph, with the `Alias:` trailer removed
    (aliases are reported separately).
    """
    text = _ALIAS_LINE_RE.sub("", _plain(help_text))
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", text)]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return "", ""
    summary = " ".join(paragraphs[0].split())
    description = "\n\n".join(" ".join(p.split()) for p in paragraphs[1:])
    return summary, description


def _make_metavar(param: click.Parameter, ctx: click.Context) -> str:
    try:
        return _plain(param.make_metavar(ctx))
    except TypeError:  # click < 8.2 takes no context
        return _plain(param.make_metavar())


def _json_default(value: Any) -> Any:
    if value is None or value is False:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, (str, int, float, bool))]
    return None


def _argument_entry(param: click.Parameter, ctx: click.Context) -> dict[str, Any]:
    return {
        "name": _make_metavar(param, ctx),
        "help": _plain(getattr(param, "help", None)),
        "required": bool(param.required),
    }


def _option_entry(
    param: click.Parameter, ctx: click.Context, global_flags: frozenset[str]
) -> dict[str, Any]:
    flags = list(param.opts) + list(param.secondary_opts)
    is_flag = bool(getattr(param, "is_flag", False)) or bool(param.secondary_opts)
    return {
        "flags": flags,
        "metavar": None if is_flag else _make_metavar(param, ctx),
        "is_flag": is_flag,
        "help": _plain(getattr(param, "help", None)),
        "default": _json_default(param.default),
        "global": bool(flags) and all(flag in global_flags for flag in flags),
    }


def _entry(
    command: click.Command,
    path: list[str],
    parent_ctx: click.Context,
    aliases: list[str],
    global_flags: frozenset[str],
) -> tuple[dict[str, Any], click.Context]:
    ctx = command.make_context(
        path[-1], [], parent=parent_ctx, resilient_parsing=True
    )
    summary, description = _split_help(command.help)
    arguments: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    for param in command.params:
        if getattr(param, "hidden", False):
            continue
        # Duck-typed: Typer >=0.16 vendors its own click fork, so its
        # TyperArgument is not an instance of the `click` package's Argument.
        if getattr(param, "param_type_name", "") == "argument":
            arguments.append(_argument_entry(param, ctx))
        else:
            options.append(_option_entry(param, ctx, global_flags))
    entry = {
        "path": list(path),
        "kind": "group" if hasattr(command, "commands") else "command",
        "aliases": aliases,
        "summary": summary,
        "description": description,
        "usage": _USAGE_PREFIX_RE.sub("", _plain(command.get_usage(ctx))),
        "arguments": arguments,
        "options": options,
        "docs_url": docs_url(path),
    }
    return entry, ctx


def build_reference(app: typer.Typer) -> dict[str, Any]:
    """Render the whole Typer app as the `docs/commands.json` document."""
    # Imported lazily: `omm.cli` imports this module for the help epilogs.
    from omm.cli import GLOBAL_FLAG_OPTS, _COMMAND_ALIASES, _hide_unsupported_global_flags

    aliases_by_command: dict[str, list[str]] = {}
    for alias, target in _COMMAND_ALIASES.items():
        aliases_by_command.setdefault(target, []).append(alias)
    for names in aliases_by_command.values():
        names.sort()

    group = typer.main.get_command(app)
    # This builds its own click command tree rather than reusing the live
    # CLI's, so it needs the same --yes/--json visibility pass the real
    # invocation gets from _RootHelpGroup.invoke - otherwise docs/commands.json
    # keeps advertising a flag `omm <cmd> --help` already hides (issue #375).
    _hide_unsupported_global_flags(group, [])
    root_ctx = click.Context(group, info_name="omm", resilient_parsing=True)

    commands: list[dict[str, Any]] = []
    for name in sorted(getattr(group, "commands", {})):
        command = group.commands[name]
        if command.hidden:
            continue
        entry, ctx = _entry(
            command, [name], root_ctx, aliases_by_command.get(name, []), GLOBAL_FLAG_OPTS
        )
        commands.append(entry)
        if name not in NESTED_GROUPS:
            continue
        for sub_name in sorted(getattr(command, "commands", {})):
            sub = command.commands[sub_name]
            if sub.hidden:
                continue
            commands.append(_entry(sub, [name, sub_name], ctx, [], GLOBAL_FLAG_OPTS)[0])

    commands.sort(key=lambda entry: entry["path"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "omm_version": omm_version(),
        "docs_base_url": DOCS_BASE_URL,
        "commands": commands,
    }


def omm_version() -> str:
    """Project version, preferring the checkout's pyproject.toml.

    `scripts/pre-commit` bumps the patch version on every commit, so this
    value is deliberately excluded from `scripts/check_docs_sync.py`'s
    staleness comparison - only the command data itself must match.
    """
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        try:
            try:
                import tomllib
            except ModuleNotFoundError:  # Python 3.10
                import tomli as tomllib  # type: ignore[no-redef]
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            version = data.get("project", {}).get("version")
            if isinstance(version, str):
                return version
        except Exception:
            pass
    try:
        from importlib.metadata import version as _version

        return _version("omm-model")
    except Exception:
        return "0.0.0"
