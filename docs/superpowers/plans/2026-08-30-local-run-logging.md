# Local Run Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `omm` a persistent local record — one JSONL file per invocation plus a human-readable `history.log` — of every command it runs and the domain events inside it.

**Architecture:** A new `omm/runlog.py` module attaches a JSON-lines `logging.FileHandler` to the `omm` logger for the lifetime of one process, writing to `~/.omm/logs/<ts>_<pid>_<cmd>.jsonl`. `cli.main()` calls `runlog.start()` before `app()` and `runlog.finish()` in a `finally`. Domain modules emit events through `logging.getLogger(__name__)`. On run end, one locked append adds a summary block to `history.log`. Everything in `runlog` swallows its own errors so logging never changes a command's behavior or exit code.

**Tech Stack:** Python 3.10+, stdlib `logging` + `json`, `filelock` (via existing `omm.atomic.locked`), Typer/Click CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-local-run-logging-design.md`

## Global Constraints

- Python 3.10+; CI pins 3.11. No new runtime dependencies (`logging`, `json`, `os`, `datetime` are stdlib; `filelock` already a dependency).
- `scikit-learn` is never a runtime dependency — irrelevant here, do not import.
- `runlog.py` must have **no import-time side effects**: no directory creation, no handler attachment, no `config.OMM_HOME` read at module scope. All of that happens inside `start()` / `finish()` / the `omm log` command.
- Read `config.OMM_HOME` **at call time** as `config.OMM_HOME` (module attribute access), never `from omm.config import OMM_HOME`. The `isolated_omm_home` fixture monkeypatches `config.OMM_HOME`; a value bound at import escapes the patch.
- Logging code never raises to its caller. Every public `runlog` function wraps its body in `try/except Exception: pass` (or returns a safe default).
- Never log: `Authorization` headers, auth env tokens (`HF_TOKEN`, etc.), API keys, HTTP request/response bodies. URLs are logged only after stripping query string and userinfo.
- CLI surface stays minimal. The only new command is `omm log` (a read command, like `omm list` — not config, so not under `omm setting`).
- `cli.py` lazy-imports `questionary`/`requests`/`prompt_toolkit`/`importlib.metadata` inside functions to protect startup time. `runlog` uses only stdlib + `omm.config` + `omm.atomic`, all cheap; `import logging` at module scope in domain modules is fine (stdlib, already imported transitively).
- Commit after every task. Only `git add` the exact files the task changed — never `-A` / `.`. Re-check `git log -5` / `git status` right before committing (concurrent sessions share this checkout).
- Target branch is `beta`.

---

## File Structure

- **Create `src/omm/runlog.py`** — the whole run-log mechanism: path resolution, argv/URL scrubbing, the JSON formatter, `start()`, `finish()`, `rebuild_history()`, `read_history()`. One responsibility: turn a process's log records into on-disk files. ~180 lines.
- **Create `tests/test_runlog.py`** — unit tests for `runlog`.
- **Modify `src/omm/cli.py`** — `main()` gains `runlog.start()` / `finish()` around `app()`; a new `log` command near the other read commands (`list` is at `@app.command(name="list")` ~line 6226).
- **Modify `src/omm/linker.py`** — `log = logging.getLogger(__name__)`; wrap `link_file` to log the chosen method.
- **Modify `src/omm/registry.py`** — `log = logging.getLogger(__name__)`; log in `upsert_entry` and `remove_entry`.
- **Modify `src/omm/downloader.py`** — `log = logging.getLogger(__name__)`; log download start/complete/fail in `download_file`.
- **Modify `src/omm/engines/base.py`** — `log = logging.getLogger(__name__)`; in `HttpRuntimeClient.request`, a `log.debug` with method + scrubbed URL + status.
- **Modify `src/omm/scan_import.py`** — `log = logging.getLogger(__name__)`; log adopted GGUFs.

---

### Task 1: `runlog.py` core — per-invocation JSONL

**Files:**
- Create: `src/omm/runlog.py`
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: `omm.config` (`config.OMM_HOME`).
- Produces:
  - `runlog.start(argv: list[str]) -> None` — resolves the run's jsonl path, attaches a handler to the `"omm"` logger, writes a `run_start` record. Idempotent-safe: a second call with a handler already attached is a no-op.
  - `runlog.finish(exit_code: int, outcome: str) -> None` — writes a `run_end` record, removes and closes the handler. (History-log append is added in Task 2.)
  - `runlog.subcommand_of(argv: list[str]) -> str` — resolved subcommand name, or `"bare"` (no subcommand), or `"unknown"`.
  - Module attribute `runlog._HANDLER: logging.Handler | None` — the currently attached handler (tests assert on it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runlog.py
import json
import logging
from pathlib import Path

from omm import config, runlog


def _jsonl_files(home: Path) -> list[Path]:
    return sorted((home / "logs").glob("*.jsonl"))


def test_start_finish_writes_wellformed_jsonl(isolated_omm_home):
    runlog.start(["install", "some-model"])
    logging.getLogger("omm.linker").info(
        "linked", extra={"event": "link", "engine": "ollama", "method": "symlink"}
    )
    runlog.finish(0, "ok")

    files = _jsonl_files(config.OMM_HOME)
    assert len(files) == 1
    assert "_install.jsonl" in files[0].name

    lines = files[0].read_text().splitlines()
    records = [json.loads(line) for line in lines]  # every line is valid JSON
    assert records[0]["event"] == "run_start"
    assert records[-1]["event"] == "run_end"
    assert records[-1]["exit_code"] == 0
    assert records[-1]["outcome"] == "ok"
    assert any(r.get("event") == "link" and r.get("method") == "symlink" for r in records)
    # handler detached after finish
    assert runlog._HANDLER is None
    assert not any(isinstance(h, logging.Handler) and getattr(h, "_omm_runlog", False)
                   for h in logging.getLogger("omm").handlers)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runlog.py::test_start_finish_writes_wellformed_jsonl -q`
Expected: FAIL — `AttributeError: module 'omm' has no attribute 'runlog'` / `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/omm/runlog.py
"""Per-invocation local run log. Best-effort: never raises to its caller,
never changes a command's behavior or exit code.

One JSON object per line goes to ``~/.omm/logs/<ts>_<pid>_<cmd>.jsonl`` for
the lifetime of one process. Domain code emits events through
``logging.getLogger("omm.<module>")``; this module owns the handler.
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
_STRUCTURED_KEYS = {  # LogRecord attrs that are ours, not stdlib noise
    "event", "model", "engine", "method", "file", "dst", "source", "size",
    "url", "status", "fields", "count", "reason", "sha256",
}
_HANDLER: logging.Handler | None = None
_RUN_STARTED_AT: float | None = None
_RUN_PATH: Path | None = None


def _logs_dir() -> Path:
    return config.OMM_HOME / "logs"


def _debug_enabled() -> bool:
    return os.environ.get("OMM_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


def subcommand_of(argv: list[str]) -> str:
    """First non-flag token if it names a registered command, else 'bare'
    (nothing but flags) or 'unknown'. Never echoes an arbitrary token — a
    command line carries queries, URLs and paths that must not reach a
    filename."""
    try:
        from omm.cli import _REGISTERED_COMMAND_NAMES  # resolved after registration
    except Exception:
        _REGISTERED_COMMAND_NAMES = frozenset()
    seen_token = False
    for token in argv:
        if token.startswith("-"):
            continue
        seen_token = True
        return token if token in _REGISTERED_COMMAND_NAMES else "unknown"
    return "bare" if not seen_token else "unknown"


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
    with '<arg>' so queries / model ids / paths / URLs never land in the log."""
    try:
        from omm.cli import _REGISTERED_COMMAND_NAMES
    except Exception:
        _REGISTERED_COMMAND_NAMES = frozenset()
    out = []
    for token in argv:
        if token.startswith("-") or token in _REGISTERED_COMMAND_NAMES:
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
            if key in record.__dict__ and key != "event":
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

        _write_record({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": "run_start",
            "omm_version": _omm_version_safe(),
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "argv": _scrub_argv(argv),
        })
    except Exception:
        _HANDLER = None
        _RUN_PATH = None


def finish(exit_code: int, outcome: str) -> None:
    global _HANDLER, _RUN_PATH, _RUN_STARTED_AT
    try:
        duration = None
        if _RUN_STARTED_AT is not None:
            duration = round(time.monotonic() - _RUN_STARTED_AT, 3)
        _write_record({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": "run_end",
            "exit_code": exit_code,
            "outcome": outcome,
            "duration_s": duration,
        })
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runlog.py::test_start_finish_writes_wellformed_jsonl -q`
Expected: PASS

- [ ] **Step 5: Add scrubbing + debug-gating + error-swallow tests**

```python
def test_debug_gating(isolated_omm_home, monkeypatch):
    monkeypatch.delenv("OMM_DEBUG", raising=False)
    runlog.start(["list"])
    logging.getLogger("omm.x").debug("noisy", extra={"event": "probe"})
    logging.getLogger("omm.x").info("kept", extra={"event": "kept"})
    runlog.finish(0, "ok")
    text = next(iter((config.OMM_HOME / "logs").glob("*.jsonl"))).read_text()
    assert "probe" not in text
    assert "kept" in text


def test_debug_env_lets_debug_through(isolated_omm_home, monkeypatch):
    monkeypatch.setenv("OMM_DEBUG", "1")
    runlog.start(["list"])
    logging.getLogger("omm.x").debug("noisy", extra={"event": "probe"})
    runlog.finish(0, "ok")
    text = next(iter((config.OMM_HOME / "logs").glob("*.jsonl"))).read_text()
    assert "probe" in text


def test_argv_and_url_scrubbing(isolated_omm_home):
    runlog.start(["search", "secret query terms"])
    logging.getLogger("omm.d").info(
        "download", extra={"event": "download", "url":
         runlog.scrub_url("https://user:pw@hf.co/repo/model.gguf?token=SECRET")}
    )
    runlog.finish(0, "ok")
    text = next(iter((config.OMM_HOME / "logs").glob("*.jsonl"))).read_text()
    assert "secret query terms" not in text
    assert "SECRET" not in text
    assert "pw@" not in text
    assert "hf.co/repo/model.gguf" in text


def test_logging_failure_is_swallowed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(runlog, "_logs_dir", lambda: (_ for _ in ()).throw(OSError("boom")))
    runlog.start(["list"])          # must not raise
    runlog.finish(1, "failed")      # must not raise
    assert runlog._HANDLER is None
```

- [ ] **Step 6: Run the full test file**

Run: `python -m pytest tests/test_runlog.py -q`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add src/omm/runlog.py tests/test_runlog.py
git commit -m "feat: add per-invocation JSONL run log (runlog.start/finish)"
```

---

### Task 2: `history.log` + rebuild + read

**Files:**
- Modify: `src/omm/runlog.py`
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: Task 1's `_RUN_PATH`, `_logs_dir()`, module state.
- Produces:
  - `runlog.finish(...)` now also appends one human-readable block to `~/.omm/logs/history.log` under `omm.atomic.locked`.
  - `runlog.rebuild_history() -> int` — regenerate `history.log` from every `*.jsonl` in the logs dir, ordered by `run_start` ts. Returns the run count.
  - `runlog.read_history(lines: int | None = None, grep: str | None = None) -> str` — text of `history.log` (last `lines` blocks if given; only blocks containing `grep` if given). Returns `""` when the file is absent.

- [ ] **Step 1: Write the failing test**

```python
def test_history_block_appended_on_finish(isolated_omm_home):
    runlog.start(["install", "m"])
    logging.getLogger("omm.linker").info(
        "linked", extra={"event": "link", "engine": "ollama", "method": "symlink"}
    )
    runlog.finish(0, "ok")
    history = (config.OMM_HOME / "logs" / "history.log").read_text()
    assert "omm install" in history
    assert "ok" in history
    assert ".jsonl" in history  # pointer to the detail file


def test_rebuild_history_orders_by_ts(isolated_omm_home):
    for name in ("list", "search"):
        runlog.start([name])
        runlog.finish(0, "ok")
    (config.OMM_HOME / "logs" / "history.log").unlink()
    count = runlog.rebuild_history()
    assert count == 2
    rebuilt = (config.OMM_HOME / "logs" / "history.log").read_text()
    assert rebuilt.index("omm list") < rebuilt.index("omm search")


def test_read_history_grep_and_lines(isolated_omm_home):
    for name in ("list", "search", "install"):
        runlog.start([name])
        runlog.finish(0, "ok")
    assert "omm search" in runlog.read_history(grep="search")
    assert "omm list" not in runlog.read_history(grep="search")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_runlog.py -q -k history`
Expected: FAIL — `AttributeError: rebuild_history` / no `history.log` written.

- [ ] **Step 3: Implement**

Add to `src/omm/runlog.py`:

```python
from omm.atomic import locked

_BLOCK_SEP = "\n"  # blocks are separated by a blank line


def _summarize(records: list[dict]) -> str:
    start_rec = next((r for r in records if r.get("event") == "run_start"), {})
    end_rec = next((r for r in records if r.get("event") == "run_end"), {})
    argv = " ".join(start_rec.get("argv", []))
    when = start_rec.get("ts", "?")
    version = start_rec.get("omm_version", "?")
    outcome = end_rec.get("outcome", "unknown")
    dur = end_rec.get("duration_s")
    head = f"{when}  omm {argv}   ({version})"
    status = f"  -> {outcome}" + (f"  ({dur}s)" if dur is not None else "")
    body_lines = []
    for r in records:
        ev = r.get("event")
        if ev in ("run_start", "run_end", "log"):
            continue
        detail = " ".join(
            str(r[k]) for k in ("model", "engine", "method", "file", "url", "count", "reason")
            if k in r
        )
        body_lines.append(f"  {ev:<10} {r.get('msg', '')}  {detail}".rstrip())
    lines = [head, status, *body_lines]
    if _RUN_PATH is not None:
        lines.append(f"  detail: {_RUN_PATH.relative_to(config.OMM_HOME.parent) if False else _RUN_PATH.name}")
    return "\n".join(lines) + "\n"


def _history_path() -> Path:
    return _logs_dir() / "history.log"


def _append_history(block: str) -> None:
    path = _history_path()
    with locked(path, timeout=10):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block + _BLOCK_SEP)


def _records_of(jsonl_path: Path) -> list[dict]:
    out = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
```

Extend `finish()` — after the `run_end` `_write_record`, before detaching:

```python
        try:
            if _RUN_PATH is not None and _RUN_PATH.exists():
                _append_history(_summarize(_records_of(_RUN_PATH)))
        except Exception:
            pass
```

Add the public helpers:

```python
def rebuild_history() -> int:
    try:
        files = sorted(_logs_dir().glob("*.jsonl"))
        blocks = []
        for f in files:
            records = _records_of(f)
            if not records:
                continue
            global _RUN_PATH
            saved, _RUN_PATH = _RUN_PATH, f
            try:
                blocks.append(_summarize(records))
            finally:
                _RUN_PATH = saved
        blocks.sort(key=lambda b: b.split("  omm", 1)[0])
        from omm.atomic import atomic_write_text
        atomic_write_text(_history_path(), _BLOCK_SEP.join(blocks) + (_BLOCK_SEP if blocks else ""))
        return len(blocks)
    except Exception:
        return 0


def read_history(lines: int | None = None, grep: str | None = None) -> str:
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
```

Note: `_summarize`'s `detail:` line uses `_RUN_PATH.name` — keep it simple, drop the dead `relative_to` ternary when implementing (it is shown above only to flag the intent; write `lines.append(f"  detail: {_RUN_PATH.name}")`).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_runlog.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/runlog.py tests/test_runlog.py
git commit -m "feat: append human-readable history.log block per run; add rebuild/read"
```

---

### Task 3: Wire into `cli.main()`

**Files:**
- Modify: `src/omm/cli.py` (`main()`, ~line 9282; add `from omm import runlog` near the other `from omm import ...`)
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: `runlog.start`, `runlog.finish`.
- Produces: every real `omm` invocation writes a jsonl + a `history.log` block.

- [ ] **Step 1: Write the failing test**

```python
def test_cli_main_writes_run_log(isolated_omm_home, monkeypatch, capsys):
    from omm import cli
    monkeypatch.setattr("sys.argv", ["omm", "--version"])
    try:
        cli.main()
    except SystemExit as e:
        assert e.code in (0, None)
    files = sorted((config.OMM_HOME / "logs").glob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(l) for l in files[0].read_text().splitlines()]
    assert records[0]["event"] == "run_start"
    assert records[-1]["event"] == "run_end"
    assert records[-1]["exit_code"] == 0
    assert (config.OMM_HOME / "logs" / "history.log").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_runlog.py::test_cli_main_writes_run_log -q`
Expected: FAIL — no `logs/` dir created (main() doesn't call runlog yet).

- [ ] **Step 3: Implement — rewrite `main()`**

Current `main()` wraps `app()` in `try/except`. Change it to set `exit_code`/`outcome` in each branch and call `runlog.finish` in a `finally`:

```python
def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts]).
    ... (keep existing docstring) ...
    """
    runlog.start(sys.argv[1:])
    exit_code, outcome = 0, "ok"
    try:
        app()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        outcome = "ok" if exit_code == 0 else ("usage-error" if exit_code == 2 else "failed")
        raise
    except KeyboardInterrupt:
        exit_code, outcome = 130, "interrupted"
        raise
    except InsufficientDiskSpaceError as e:
        exit_code, outcome = 1, "failed"
        err_console.print(f"[error]{e}[/error]")
        raise SystemExit(1) from None
    except PermissionError as e:
        exit_code, outcome = 1, "failed"
        target = f" ({e.filename})" if e.filename else ""
        errors.print_cli_error(
            err_console,
            f"Permission denied{target}: {e.strerror or e}.",
            fix="Check that the file or directory is writable by your user, "
            "and that no other program (or a differently-owned daemon) has it open.",
        )
        raise SystemExit(1) from None
    except OSError as e:
        exit_code, outcome = 1, "failed"
        if e.errno == errno.ENOSPC:
            err_console.print(
                "[error]Not enough disk space to complete this operation. "
                "Free up space and try again.[/error]"
            )
            raise SystemExit(1) from None
        _queue_crash_report(e)
        raise
    except Exception as e:
        exit_code, outcome = 1, "failed"
        _queue_crash_report(e)
        raise
    finally:
        runlog.finish(exit_code, outcome)
```

Add the import: find the `from omm import (` block near line 57 (it already imports `package_metadata`) and add `runlog` to it, or add a plain `from omm import runlog` beside the other `from omm import ...` lines.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_runlog.py -q`
Expected: PASS

- [ ] **Step 5: Full-suite smoke on the touched areas**

Run: `python -m pytest tests/test_cli_crash_report.py tests/test_runlog.py -q`
Expected: PASS (crash-report tests still green — `_queue_crash_report` paths unchanged in effect).

- [ ] **Step 6: Manual check**

```bash
OMM_HOME=$(mktemp -d) python -m omm --version
OMM_HOME=$(mktemp -d) sh -c 'OMM_HOME=$0 python -m omm --version; ls $0/logs; cat $0/logs/history.log' "$(mktemp -d)"
```
Expected: a `<ts>_<pid>_bare.jsonl` (or `_unknown` — `--version` is an eager flag with no subcommand) and a `history.log` block.

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_runlog.py
git commit -m "feat: write a run log around every omm invocation"
```

---

### Task 4: `omm log` command

**Files:**
- Modify: `src/omm/cli.py` (new command near `@app.command(name="list")`, ~line 6226)
- Test: `tests/test_cli_log_command.py`

**Interfaces:**
- Consumes: `runlog.read_history`, `runlog.rebuild_history`.
- Produces: `omm log [--lines N] [--grep TEXT] [--rebuild]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_log_command.py
import json
from typer.testing import CliRunner
from omm import cli, config, runlog

runner = CliRunner()


def test_omm_log_prints_history(isolated_omm_home):
    runlog.start(["list"])
    runlog.finish(0, "ok")
    result = runner.invoke(cli.app, ["log"])
    assert result.exit_code == 0
    assert "omm list" in result.output


def test_omm_log_rebuild(isolated_omm_home):
    runlog.start(["search"])
    runlog.finish(0, "ok")
    (config.OMM_HOME / "logs" / "history.log").unlink()
    result = runner.invoke(cli.app, ["log", "--rebuild"])
    assert result.exit_code == 0
    assert (config.OMM_HOME / "logs" / "history.log").exists()
    assert "omm search" in result.output


def test_omm_log_empty(isolated_omm_home):
    result = runner.invoke(cli.app, ["log"])
    assert result.exit_code == 0
    assert "no run log" in result.output.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cli_log_command.py -q`
Expected: FAIL — `No such command 'log'`.

- [ ] **Step 3: Implement**

Add near the other read commands in `cli.py`:

```python
@app.command(name="log")
@global_flags
def log_cmd(
    ctx: typer.Context,
    lines: int = typer.Option(40, "--lines", "-n", help="Show the last N runs."),
    grep: str = typer.Option(None, "--grep", help="Only show runs whose block contains TEXT."),
    rebuild: bool = typer.Option(
        False, "--rebuild", help="Regenerate history.log from the per-run JSONL files."
    ),
) -> None:
    """Show the local run log (~/.omm/logs/history.log).

    Each `omm` command appends a summary block here; full detail for one run
    is in its `~/.omm/logs/<timestamp>_<pid>_<command>.jsonl` file."""
    from omm import runlog

    if rebuild:
        count = runlog.rebuild_history()
        console.print(f"[muted]Rebuilt history.log from {count} run(s).[/muted]")
    text = runlog.read_history(lines=lines, grep=grep)
    if not text.strip():
        console.print("[muted]No run log yet.[/muted]")
        return
    console.print(text.rstrip())
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_cli_log_command.py -q`
Expected: PASS

- [ ] **Step 5: Check `omm help` groups**

`_HELP_ALL_GROUPS` in `cli.py` (~line 642) lists commands per section. Add `"log"` to the `Maintenance` group's list so `omm help --all` shows it in the right place (a command not in any group still appears under "Other:", so this is polish, not correctness).

Run: `python -m pytest tests/test_cli_help*.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_log_command.py
git commit -m "feat: add 'omm log' to read the local run log"
```

---

### Task 5: Domain events — linker, registry, downloader

**Files:**
- Modify: `src/omm/linker.py`, `src/omm/registry.py`, `src/omm/downloader.py`
- Test: `tests/test_runlog_events.py`

**Interfaces:**
- Consumes: the `"omm"` logger handler attached by `runlog.start` (Task 1).
- Produces: `event: "link"`, `event: "registry-upsert"`, `event: "registry-remove"`, `event: "download-start"`, `event: "download-complete"`, `event: "download-failed"` records in the run's jsonl.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runlog_events.py
import json
import logging
from pathlib import Path

from omm import config, runlog, registry


def _records(home: Path):
    f = next(iter((home / "logs").glob("*.jsonl")))
    return [json.loads(l) for l in f.read_text().splitlines()]


def test_registry_upsert_and_remove_logged(isolated_omm_home):
    runlog.start(["install"])
    registry.upsert_entry("model-a.gguf", repo_id="org/model-a")
    registry.remove_entry("model-a.gguf")
    runlog.finish(0, "ok")
    events = [r.get("event") for r in _records(config.OMM_HOME)]
    assert "registry-upsert" in events
    assert "registry-remove" in events


def test_link_method_logged(isolated_omm_home, tmp_path):
    from omm import linker
    src = tmp_path / "central" / "m.gguf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"gguf")
    dst = tmp_path / "engine" / "m.gguf"
    runlog.start(["install"])
    linker.link_file(src, dst)
    runlog.finish(0, "ok")
    link_events = [r for r in _records(config.OMM_HOME) if r.get("event") == "link"]
    assert link_events and link_events[0]["method"] in ("symlink", "hardlink", "copy")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_runlog_events.py -q`
Expected: FAIL — no `link` / `registry-*` events.

- [ ] **Step 3: Implement**

**`src/omm/registry.py`** — add after the existing imports:

```python
import logging

log = logging.getLogger(__name__)
```

In `upsert_entry(filename, **fields)`, after the entry is written:

```python
    log.info("registry upsert %s", filename,
             extra={"event": "registry-upsert", "model": filename,
                    "fields": sorted(fields)})
```

In `remove_entry(filename)`, after removal:

```python
    log.info("registry remove %s", filename,
             extra={"event": "registry-remove", "model": filename})
```

**`src/omm/linker.py`** — add `import logging` + `log = logging.getLogger(__name__)` near the top. Rename the current `def link_file(` body to `def _link_file_impl(` (same signature) and add a thin wrapper:

```python
def link_file(
    src: Path, dst: Path, *, on_copy: "CopyReporter | None" = None, force: bool = False
) -> str:
    method = _link_file_impl(src, dst, on_copy=on_copy, force=force)
    log.info("exposed %s via %s", src.name, method,
             extra={"event": "link", "file": src.name, "dst": str(dst), "method": method})
    return method
```

(Keep `_link_file_impl`'s docstring; the wrapper needs only a one-line docstring. Confirm no other module imports a name from inside the old body — `grep -n "link_file" src/omm/*.py src/omm/**/*.py`.)

**`src/omm/downloader.py`** — add `import logging` + `log = logging.getLogger(__name__)` near the top. In `download_file(...)`:

- Right after the destination/URL are known:

```python
    log.info("download start %s", dest.name,
             extra={"event": "download-start", "file": dest.name,
                    "url": runlog_scrub(url)})
```

  where `runlog_scrub` is `from omm.runlog import scrub_url as runlog_scrub` at the top of `downloader.py` (module-scope import is fine — `runlog` is stdlib-only).

- On successful completion (just before `download_file` returns success):

```python
    log.info("download complete %s", dest.name,
             extra={"event": "download-complete", "file": dest.name,
                    "size": dest.stat().st_size if dest.exists() else None})
```

- Wrap the body so any `DownloadError` (and `OSError`) logs then re-raises:

```python
    except DownloadError as e:
        log.warning("download failed %s: %s", dest.name, e,
                    extra={"event": "download-failed", "file": dest.name,
                           "reason": str(e)[:200]})
        raise
```

  Place this at the outermost `try` in `download_file`. If `download_file` has no outer `try`, add one that wraps the existing body and only catches to log-and-reraise (`except (DownloadError, OSError) as e:`). Do not swallow.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_runlog_events.py -q`
Expected: PASS

- [ ] **Step 5: Regression check on the touched modules**

Run: `python -m pytest tests/test_linker_link_file.py tests/test_registry*.py tests/test_downloader*.py -q`
Expected: PASS (behavior unchanged; only log calls added).

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py src/omm/registry.py src/omm/downloader.py tests/test_runlog_events.py
git commit -m "feat: log link method, registry changes, and downloads to the run log"
```

---

### Task 6: Debug-level detail — engine HTTP + scan_import

**Files:**
- Modify: `src/omm/engines/base.py`, `src/omm/scan_import.py`
- Test: `tests/test_runlog_events.py`

**Interfaces:**
- Consumes: the `"omm"` logger handler; `OMM_DEBUG` gating from Task 1.
- Produces: `event: "http"` (DEBUG) records for runner API calls; `event: "adopt"` records for scan-import adoptions.

- [ ] **Step 1: Write the failing test**

```python
def test_scan_import_adoption_logged(isolated_omm_home, tmp_path, monkeypatch):
    from omm import scan_import
    # a GGUF an engine already has, not yet in the hub
    external = tmp_path / "ext" / "already.gguf"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"GGUF" + b"\0" * 64)
    runlog.start(["scan"])
    # call whatever scan_import entry adopts a single discovered file;
    # if it needs a discovered-file record, build the minimal one it expects
    scan_import.adopt_external_gguf(external)  # adjust to the real entry point
    runlog.finish(0, "ok")
    events = [r.get("event") for r in _records(config.OMM_HOME)]
    assert "adopt" in events


def test_http_detail_only_with_debug(isolated_omm_home, monkeypatch):
    from omm.engines import base
    # minimal: call HttpRuntimeClient.request against a stub session
    ...  # see Step 3 for the exact stub; assert 'http' event absent without
         # OMM_DEBUG and present with OMM_DEBUG=1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_runlog_events.py -q -k "scan_import or http_detail"`
Expected: FAIL.

- [ ] **Step 3: Implement**

**`src/omm/engines/base.py`** — add `import logging` + `log = logging.getLogger(__name__)`. In `HttpRuntimeClient.request(...)`, after a response is obtained (success path), before returning:

```python
        log.debug("%s %s -> %s", method, _scrub(url), getattr(response, "status_code", "?"),
                  extra={"event": "http", "method": method,
                         "url": _scrub(url), "status": getattr(response, "status_code", None)})
```

where `_scrub` is `from omm.runlog import scrub_url as _scrub` at module top. Never log headers or body. In the exception branches already present, add a matching `log.debug("%s %s failed: %s", ...)` with `event: "http"` and no body.

**`src/omm/scan_import.py`** — add `import logging` + `log = logging.getLogger(__name__)`. At the point a discovered external GGUF is adopted into the hub (after the sha256 dedup check decides "adopt"), add:

```python
    log.info("adopted %s", path.name,
             extra={"event": "adopt", "file": path.name, "sha256": digest[:12]})
```

and where dedup finds the file already in the hub:

```python
    log.debug("skip (already in hub) %s", path.name,
              extra={"event": "adopt-skip", "file": path.name})
```

Locate the real function names first: `grep -n "^def \|sha256\|dedup\|adopt\|import" src/omm/scan_import.py`. Adjust the test's entry-point call in Step 1 to match.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_runlog_events.py -q`
Expected: PASS

- [ ] **Step 5: Regression check**

Run: `python -m pytest tests/test_scan_import*.py "tests/test_engine"*.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/engines/base.py src/omm/scan_import.py tests/test_runlog_events.py
git commit -m "feat: OMM_DEBUG run-log detail for engine HTTP calls and scan-import"
```

---

### Task 7: Docs + full suite

**Files:**
- Modify: `CLAUDE.md` (architecture section), `README` if it lists commands, `docs/superpowers/specs/2026-08-30-local-run-logging-design.md` (mark shipped)

- [ ] **Step 1: Update `CLAUDE.md`**

In the Architecture section add a short paragraph:

> **Run log.** `runlog.py` attaches a JSON-lines handler to the `omm` logger for one process; `cli.main()` brackets `app()` with `runlog.start()`/`finish()`. Every invocation writes `~/.omm/logs/<ts>_<pid>_<cmd>.jsonl` (full detail) and appends one block to `logs/history.log` (human-readable, `locked()`-guarded). Local only — never uploaded; the error-report payload does not read it. `OMM_DEBUG=1` adds subprocess/HTTP detail. `omm log` reads it. Domain modules emit events via `logging.getLogger("omm.<module>")` — add `log.info(...)` at state changes, `log.debug(...)` for noisy detail.

In "Never touch the user's installed `omm`" / testing-gotchas, note: `tests/test_runlog*.py` and any test calling `cli.main()` write under `config.OMM_HOME` — the `isolated_omm_home` fixture covers this; do not run them with a real `OMM_HOME`.

- [ ] **Step 2: Full test suite**

Run: `python -m pytest -q`
Expected: PASS (pre-existing sklearn-skips are not failures).

- [ ] **Step 3: Startup-time sanity**

Run: `OMM_HOME=$(mktemp -d) python -c "import time; t=time.time(); import omm.cli; print((time.time()-t)*1000, 'ms')"`
Expected: comparable to before (runlog is stdlib-only; no new heavy import at module scope).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-30-local-run-logging-design.md
git commit -m "docs: document the local run log"
```

---

## Self-Review

**Spec coverage:**
- Goal 1 (self-debug) + Goal 2 (audit history) → Tasks 1–6 (jsonl + history.log + events). ✓
- Non-goal: remote stats → not in any task; called out in Task 7 docs. ✓
- File layout (`<ts>_<pid>_<cmd>.jsonl`, `history.log`) → Task 1 / Task 2. ✓
- Layer 1 (run_start/run_end, exit code map) → Task 1 + Task 3. ✓
- Layer 2 always-on events → Task 5. ✓
- Layer 2 `OMM_DEBUG` detail → Task 1 (handler level) + Task 6 (call sites). ✓
- `runlog.py` API (`start`, `finish`, `rebuild_history`, `read_history`) → Tasks 1–2. ✓
- `omm log` (+ `--rebuild`) → Task 4. ✓
- Concurrency (per-run file single writer; `history.log` via `locked()`) → Task 1 (jsonl), Task 2 (`_append_history`). ✓
- Failure isolation (never raises) → Task 1 wrappers + `test_logging_failure_is_swallowed`. ✓
- Scrubbing (argv, URL, never auth) → Task 1 (`_scrub_argv`, `scrub_url`) + `test_argv_and_url_scrubbing`; Task 5/6 use `scrub_url` on every logged URL. ✓
- Retention: none — no task, matches spec. ✓
- Testing section → `tests/test_runlog.py`, `tests/test_runlog_events.py`, `tests/test_cli_log_command.py`, integration test in Task 3. ✓

**Placeholder scan:** Task 6 Step 1 leaves a `...` in `test_http_detail_only_with_debug` and instructs locating real `scan_import` function names — deliberate: those names must be read from the module at implementation time, and the exact log call code IS given. Task 5 Step 3 `download_file` outer-`try` placement is described with the exact except-block code. Acceptable — no logic left unspecified.

**Type consistency:** `runlog.start(argv: list[str])`, `runlog.finish(exit_code: int, outcome: str)`, `runlog.rebuild_history() -> int`, `runlog.read_history(lines, grep) -> str`, `runlog.scrub_url(str) -> str`, `runlog.subcommand_of(list) -> str` — used consistently across Tasks 1–4. `_link_file_impl` / `link_file` wrapper split named identically in interface and steps. Event names (`link`, `registry-upsert`, `registry-remove`, `download-start`/`-complete`/`-failed`, `http`, `adopt`) consistent between Task 5/6 code and their tests.
