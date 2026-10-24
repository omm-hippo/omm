"""`omm rag mcp|connect|disconnect`: local document search for coding agents.

Kept out of cli.py so the document-search core and this agent bridge can grow
independently; cli.py only calls :func:`register` on its `rag` group.
"""
from __future__ import annotations

import typer

_HARNESS_HELP = "claude-code, codex, pi or cursor."


def _fail(message: str) -> None:
    from omm.cli import err_console
    from rich.markup import escape
    err_console.print(f"[error]{escape(message)}[/error]")
    raise typer.Exit(1)


def mcp() -> None:
    """Run the local document search MCP server on stdin/stdout (started by a coding agent)."""
    from omm import rag_mcp
    raise typer.Exit(rag_mcp.serve())


def connect(harness: str = typer.Argument(..., help=_HARNESS_HELP)) -> None:
    """Register OMM document search as an MCP server in a coding agent, backing up its settings."""
    from omm import rag_connect
    from omm.cli import console
    from rich.markup import escape
    try:
        result = rag_connect.connect(harness)
    except (rag_connect.RagConnectionError, OSError) as error:
        _fail(str(error) if isinstance(error, rag_connect.RagConnectionError) else "Could not access configuration files. Check permissions.")
    if result["state"] == "unchanged":
        console.print(f"{escape(harness)} already has the OMM document search MCP server ({escape(result['path'])}).")
        return
    console.print(f"Registered MCP server '{rag_connect.SERVER_NAME}' for {escape(harness)} in {escape(result['path'])}.")
    console.print("Start a new session in the harness. Documents and questions stay on this computer.")
    if harness == "claude-code":
        console.print("If Claude Code is running, restart it so it does not overwrite the new entry.")


def disconnect(harness: str = typer.Argument(..., help=_HARNESS_HELP)) -> None:
    """Remove only the OMM MCP entry from a coding agent and keep other settings."""
    from omm import rag_connect
    from omm.cli import console
    try:
        result = rag_connect.disconnect(harness)
    except (rag_connect.RagConnectionError, OSError) as error:
        _fail(str(error) if isinstance(error, rag_connect.RagConnectionError) else "Could not access configuration files. Backup retained.")
    console.print({
        "restored": "Original settings restored.",
        "removed": "Removed the OMM MCP entry; other settings were kept.",
        "absent": "The OMM MCP entry was already gone; nothing changed.",
        "not_connected": "OMM did not register an MCP server for this harness; nothing changed.",
    }[result["state"]])


def register(group: typer.Typer) -> None:
    group.command(name="mcp")(mcp)
    group.command(name="connect")(connect)
    group.command(name="disconnect")(disconnect)
