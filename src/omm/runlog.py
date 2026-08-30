"""Per-invocation local run log. Best-effort: never raises to its caller,
never changes a command's behavior or exit code.

One JSON object per line goes to ``~/.omm/logs/<ts>_<pid>_<cmd>.jsonl`` for
the lifetime of one process. Domain code emits events through
``logging.getLogger("omm.<module>")``; this module owns the handler.

Local only. Nothing here is ever uploaded, and the opt-in error-report
payload deliberately does not read these files.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from omm import config

_LOGGER_NAME = "omm"

# LogRecord attributes that are ours (passed via ``extra=``), not stdlib
# noise. ``event`` is handled separately because it always appears.
_STRUCTURED_KEYS = {
    "model", "engine", "method", "file", "dst", "source", "size",
    "url", "status", "fields", "count", "reason", "sha256",
}

_HANDLER: logging.Handler | None = None
_RUN_STARTED_AT: float | None = None
_RUN_PATH: Path | None = None

_BLOCK_SEP = "\n"  # a blank line separates history.log blocks


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _logs_dir() -> Path:
    return config.OMM_HOME / "logs"


def _debug_enabled() -> bool:
    return os.environ.get("OMM_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


def _registered_command_names() -> frozenset[str]:
    try:
        from omm.cli import _REGISTERED_COMMAND_NAMES

        return _REGISTERED_COMMAND_NAMES
    except Exception:
        return frozenset()


def subcommand_of(argv: list[str]) -> str:
    """First non-flag token if it names a registered command, else ``bare``
    (nothing but flags) or ``unknown``. Never echoes an arbitrary token — a
    command line carries queries, URLs and paths that must not reach a
    filename."""
    known = _registered_command_names()
    for token in argv:
        if token.startswith("-"):
            continue
        return token if token in known else "unknown"
    return "bare"


def scrub_url(value: str) -> str:
    """Drop query string and userinfo; keep scheme/host/path."""
    try:
        parts = urlsplit(value)
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        return "<unparseable-url>"


def _scrub_argv(argv: list[str]) -> list[str]:
    """Keep flags and the registered subcommand; replace every other token
    with ``<arg>`` so queries / model ids / paths / URLs never land in the
    log."""
    known = _registered_command_names()
    out = []
    for token in argv:
        if token.startswith("-") or token in known:
            out.append(token)
        else:
            out.append("<arg>")
    return out


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }
        for key in _STRUCTURED_KEYS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _write_record(fields: dict) -> None:
    if _RUN_PATH is None:
        return
    line = json.dumps(fields, ensure_ascii=False, default=str)
    with _RUN_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _omm_version_safe() -> str:
    try:
        from omm.cli import _omm_version

        return _omm_version()
    except Exception:
        return "unknown"


def start(argv: list[str]) -> None:
    """Resolve the run's jsonl path, attach a JSON handler to the ``omm``
    logger, write the ``run_start`` record. Called once from ``cli.main()``.
    A second call while a handler is attached is a no-op. Swallows all
    errors."""
    global _HANDLER, _RUN_STARTED_AT, _RUN_PATH
    if _HANDLER is not None:
        return
    try:
        _RUN_STARTED_AT = time.monotonic()
        cmd = subcommand_of(argv)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        directory = _logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _RUN_PATH = directory / f"{stamp}_{os.getpid()}_{cmd}.jsonl"

        handler = logging.FileHandler(_RUN_PATH, encoding="utf-8", delay=True)
        handler.setLevel(logging.DEBUG if _debug_enabled() else logging.INFO)
        handler.setFormatter(_JsonlFormatter())
        handler._omm_runlog = True  # marker for teardown / tests
        logger = logging.getLogger(_LOGGER_NAME)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > handler.level:
            logger.setLevel(handler.level)
        _HANDLER = handler

        _write_record(
            {
                "ts": _now_iso(),
                "event": "run_start",
                "omm_version": _omm_version_safe(),
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "argv": _scrub_argv(argv),
            }
        )
    except Exception:
        _HANDLER = None
        _RUN_PATH = None


def finish(exit_code: int, outcome: str) -> None:
    """Write ``run_end``, append one ``history.log`` block, detach and close
    the handler. Swallows all errors."""
    global _HANDLER, _RUN_PATH, _RUN_STARTED_AT
    try:
        duration = None
        if _RUN_STARTED_AT is not None:
            duration = round(time.monotonic() - _RUN_STARTED_AT, 3)
        _write_record(
            {
                "ts": _now_iso(),
                "event": "run_end",
                "exit_code": exit_code,
                "outcome": outcome,
                "duration_s": duration,
            }
        )
    except Exception:
        pass
    try:
        if _RUN_PATH is not None and _RUN_PATH.exists():
            _append_history(
                _summarize(_records_of(_RUN_PATH), detail_name=_RUN_PATH.name)
            )
    except Exception:
        pass
    finally:
        try:
            if _HANDLER is not None:
                logging.getLogger(_LOGGER_NAME).removeHandler(_HANDLER)
                _HANDLER.close()
        except Exception:
            pass
        _HANDLER = None
        _RUN_PATH = None
        _RUN_STARTED_AT = None


# --- history.log -----------------------------------------------------------


def _history_path() -> Path:
    return _logs_dir() / "history.log"


def _records_of(jsonl_path: Path) -> list[dict]:
    out: list[dict] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _summarize(records: list[dict], detail_name: str | None = None) -> str:
    start_rec = next((r for r in records if r.get("event") == "run_start"), {})
    end_rec = next((r for r in records if r.get("event") == "run_end"), {})
    argv = " ".join(start_rec.get("argv", []))
    when = start_rec.get("ts", "?")
    version = start_rec.get("omm_version", "?")
    outcome = end_rec.get("outcome", "unknown")
    dur = end_rec.get("duration_s")

    head = f"{when}  omm {argv}   ({version})"
    status = f"  -> {outcome}" + (f"  ({dur}s)" if dur is not None else "")
    lines = [head, status]
    for r in records:
        ev = r.get("event")
        if ev in ("run_start", "run_end", "log", None):
            continue
        detail = " ".join(
            str(r[k])
            for k in ("model", "engine", "method", "file", "url", "count", "reason")
            if k in r
        )
        lines.append(f"  {ev:<16} {r.get('msg', '')}  {detail}".rstrip())
    if detail_name:
        lines.append(f"  detail: {detail_name}")
    return "\n".join(lines) + "\n"


def _append_history(block: str) -> None:
    from omm.atomic import locked

    path = _history_path()
    with locked(path, timeout=10):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block + _BLOCK_SEP)


def rebuild_history() -> int:
    """Regenerate ``history.log`` from every ``*.jsonl`` in the logs dir,
    ordered by ``run_start`` ts. Returns the run count. Swallows errors
    (returns 0)."""
    try:
        from omm.atomic import atomic_write_text

        blocks = []
        for f in sorted(_logs_dir().glob("*.jsonl")):
            records = _records_of(f)
            if not records:
                continue
            blocks.append(_summarize(records, detail_name=f.name))
        blocks.sort(key=lambda b: b.split("  omm", 1)[0])
        atomic_write_text(
            _history_path(),
            _BLOCK_SEP.join(blocks) + (_BLOCK_SEP if blocks else ""),
        )
        return len(blocks)
    except Exception:
        return 0


def read_history(lines: int | None = None, grep: str | None = None) -> str:
    """Text of ``history.log`` — the last ``lines`` blocks if given, only
    blocks containing ``grep`` if given. ``""`` when the file is absent."""
    try:
        text = _history_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if grep:
        blocks = [b for b in blocks if grep in b]
    if lines:
        blocks = blocks[-lines:]
    return "\n\n".join(blocks) + ("\n" if blocks else "")
