"""Register the local document MCP server in coding-agent harness configs.

Only one entry, ``omm-documents``, is ever added or removed. Each harness's
original configuration bytes are kept in a private journal under
``OMM_HOME/rag-connections`` before the first write; ``disconnect`` restores
them exactly when nothing else changed and otherwise removes only our entry,
keeping every edit the user or the harness made since.

Formats (checked against each harness's documentation, 2026-10):

* Claude Code: user scope, top-level ``mcpServers`` in ``~/.claude.json``
  (``$CLAUDE_CONFIG_DIR/.claude.json`` when set), the same place
  ``claude mcp add --scope user`` writes.
* Codex: ``[mcp_servers.<name>]`` in ``~/.codex/config.toml`` (``$CODEX_HOME``).
* pi: ``mcpServers`` in ``~/.pi/agent/mcp.json`` (``$PI_CODING_AGENT_DIR``);
  native MCP since pi 0.99.
* Cursor: ``mcpServers`` in ``~/.cursor/mcp.json``.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys

from omm import atomic, config
# Shared, reviewed helpers for safe harness configuration edits.
from omm.agent_connection import _decode, _dump, _encode, _parse, _private_journal_directory, _read, _safe, _store, _write

SERVER_NAME = "omm-documents"
HARNESSES = ("claude-code", "codex", "pi", "cursor")
# ~/.claude.json also holds Claude Code's project history and grows large.
MAX_CONFIG_BYTES = 64 * 1024 * 1024


class RagConnectionError(ValueError):
    """An actionable error that does not contain configuration values."""


def _check(harness):
    if harness not in HARNESSES:
        raise RagConnectionError("Choose " + ", ".join(HARNESSES) + ".")


def config_path(harness) -> Path:
    _check(harness)
    home = Path.home()
    if harness == "claude-code":
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(override) / ".claude.json" if override else home / ".claude.json"
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml"
    if harness == "pi":
        return Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi" / "agent") / "mcp.json"
    return home / ".cursor" / "mcp.json"


def _container(harness):
    return "mcp_servers" if harness == "codex" else "mcpServers"


def installed(harness) -> bool:
    """CLI on PATH, or the harness's own configuration directory exists."""
    _check(harness)
    commands = {"claude-code": ("claude",), "codex": ("codex",), "pi": ("pi",), "cursor": ("cursor", "cursor-agent")}[harness]
    if any(shutil.which(command) for command in commands):
        return True
    path = config_path(harness)
    directory = Path.home() / ".claude" if harness == "claude-code" and not os.environ.get("CLAUDE_CONFIG_DIR") else path.parent
    return directory.is_dir()


def server_command() -> list[str]:
    """Absolute command that starts this installation's MCP server."""
    # Absolute but not resolved: a POSIX venv python is a symlink to the base
    # interpreter, and resolving it would drop the venv (and omm) from the path.
    executable = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        # npm / portable builds are a single executable with the omm CLI.
        return [executable, "rag", "mcp"]
    # A Python install (pipx, pip, editable): the module entry point skips the
    # CLI start-up prelude (update check, queued uploads), so nothing but
    # protocol messages can reach stdout and start-up stays fast.
    return [executable, "-m", "omm.rag_mcp"]


def server_entry(harness) -> dict:
    command, *args = server_command()
    entry = {"command": command, "args": args}
    if harness in ("claude-code", "cursor"):
        entry = {"type": "stdio", **entry}
    if os.environ.get("OMM_HOME"):
        # The harness starts the server without this shell's environment.
        entry["env"] = {"OMM_HOME": str(config.OMM_HOME)}
    elif harness == "claude-code":
        entry["env"] = {}
    if harness == "codex":
        entry["startup_timeout_sec"] = 30
    if harness == "pi":
        entry["description"] = "Search the user's documents registered in OMM on this computer, with exact quotes and pages."
    return entry


def _plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else value


def _keep_trailing_newlines(output: bytes, before: bytes) -> bytes:
    return output.rstrip(b"\r\n") + before[len(before.rstrip(b"\r\n")):]


def _journal(harness):
    return config.OMM_HOME / "rag-connections" / (harness + ".json")


def _load_journal(journal, harness):
    try:
        data = json.loads(_read(journal, max_bytes=MAX_CONFIG_BYTES * 2).decode("utf-8"))
        if data.get("schema_version") != 1 or data.get("harness") != harness or data.get("server") != SERVER_NAME:
            raise ValueError("journal")
        _decode(data["before"])
        if not isinstance(data["entry"], dict) or not isinstance(data["path"], str):
            raise ValueError("journal")
        return data
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RagConnectionError("OMM connection record is invalid; it was kept for manual recovery.") from error


def _entry_of(document, container):
    servers = document.get(container)
    if servers is None:
        return False, None
    if not hasattr(servers, "keys"):
        raise RagConnectionError(f"'{container}' in the harness configuration is not an object; no settings changed.")
    return (SERVER_NAME in servers), (_plain(servers[SERVER_NAME]) if SERVER_NAME in servers else None)


def connect(harness) -> dict:
    """Add or refresh our entry. Returns {"path", "state": added|updated|unchanged}."""
    _check(harness)
    if not installed(harness):
        raise RagConnectionError(f"{harness} was not found (no command on PATH and no configuration folder). Install it first.")
    journal = _journal(harness)
    _private_journal_directory(journal)
    with atomic.locked(journal):
        record = _load_journal(journal, harness) if journal.exists() else None
        path = Path(record["path"]) if record else config_path(harness)
        is_toml = path.suffix == ".toml"
        raw = _read(path, max_bytes=MAX_CONFIG_BYTES)
        document = _parse(raw, is_toml)
        container = _container(harness)
        present, existing = _entry_of(document, container)
        entry = server_entry(harness)
        if present and existing == entry:
            return {"path": str(path), "state": "unchanged"}
        if present and record is None:
            raise RagConnectionError(f"An '{SERVER_NAME}' MCP entry that OMM did not add already exists; it was left unchanged.")
        if container not in document:
            if is_toml:
                import tomlkit
                document[container] = tomlkit.table(is_super_table=True)
            else:
                document[container] = {}
        document[container][SERVER_NAME] = deepcopy(entry)
        applied = _dump(document, is_toml)
        if record is None:
            record = {"schema_version": 1, "harness": harness, "server": SERVER_NAME, "path": str(path),
                      "before": _encode(raw), "container_existed": raw is not None and container in _parse(raw, is_toml),
                      "mode": path.stat().st_mode & 0o777 if raw is not None else None}
        record.update(entry=entry, applied=_encode(applied))
        # The original bytes are persisted before the configuration changes.
        _store(journal, record)
        if _read(path, max_bytes=MAX_CONFIG_BYTES) != raw:
            raise RagConnectionError("The configuration changed while connecting; run the command again.")
        try:
            _write(path, applied)
        except OSError as error:
            raise RagConnectionError("Could not write the harness configuration; nothing else changed.") from error
    return {"path": str(path), "state": "updated" if present else "added"}


def disconnect(harness) -> dict:
    """Remove only our entry. Returns {"path", "state": restored|removed|absent|not_connected}."""
    _check(harness)
    journal = _journal(harness)
    _safe(journal)
    if not journal.exists():
        return {"path": str(config_path(harness)), "state": "not_connected"}
    with atomic.locked(journal):
        record = _load_journal(journal, harness)
        path = Path(record["path"])
        is_toml = path.suffix == ".toml"
        current = _read(path, max_bytes=MAX_CONFIG_BYTES)
        before = _decode(record["before"])
        if current is not None and current == _decode(record.get("applied")):
            # Nothing else touched the file: put the original bytes back.
            output, state = before, "restored"
        else:
            document = _parse(current, is_toml)
            container = _container(harness)
            present, existing = _entry_of(document, container)
            if not present:
                journal.unlink()
                return {"path": str(path), "state": "absent"}
            if existing != record["entry"]:
                raise RagConnectionError(f"The '{SERVER_NAME}' entry was edited after OMM added it; it was left in place. "
                                         "Remove it by hand or run connect again first.")
            del document[container][SERVER_NAME]
            if not document[container] and not record.get("container_existed"):
                del document[container]
            output = None if before is None and not document else _dump(document, is_toml)
            if output is not None and before is not None:
                # Removing a table can leave the blank line that once separated it.
                output = _keep_trailing_newlines(output, before)
            state = "removed"
        if _read(path, max_bytes=MAX_CONFIG_BYTES) != current:
            raise RagConnectionError("The configuration changed while disconnecting; run the command again.")
        try:
            _write(path, output)
            if output is not None and record.get("mode") is not None and os.name != "nt":
                path.chmod(record["mode"])
        except OSError as error:
            raise RagConnectionError("Could not write the harness configuration; the OMM record was kept.") from error
        journal.unlink()
    return {"path": str(path), "state": state}


def status(harness) -> dict:
    _check(harness)
    journal = _journal(harness)
    _safe(journal)
    record = _load_journal(journal, harness) if journal.exists() else None
    path = Path(record["path"]) if record else config_path(harness)
    document = _parse(_read(path, max_bytes=MAX_CONFIG_BYTES), path.suffix == ".toml")
    present, existing = _entry_of(document, _container(harness))
    return {"harness": harness, "path": str(path), "installed": installed(harness), "managed": record is not None,
            "registered": present, "current_command": present and existing == server_entry(harness)}
