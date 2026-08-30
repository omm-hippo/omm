# Local run logging — design

Date: 2026-08-30
Status: approved for implementation
Branch target: `beta`

## Goal

Give `omm` a persistent local record of what it did on every run, so that:

1. **The user can self-debug** — "why is this symlink broken?", "what did I install
   yesterday?", "what did the last `omm upgrade` actually touch?"
2. **Install/action history is auditable** — apt `history.log` style: when, what
   command, what changed, success or failure.

The record lives only on the user's machine. It never leaves over the network.

## Non-goals (explicitly out of scope)

- **Remote version/usage statistics** ("how many people run which version"). That is
  a separate phone-home feature with its own spec. It must be opt-in per the
  standing `CLAUDE.md` rule and can partly reuse the existing
  `telemetry.py` → cf-worker → Firebase path. Not touched here.
- **`localfit-server` (FastAPI) logging.** This spec covers the `omm` CLI only.
- **Structured metrics / spans / a logging framework.** stdlib `logging` only.
- **Auto-upload of these logs by `omm contribute` or the crash reporter.** The
  error-report path keeps building its own payload; it does not read these files.
  Keeping that boundary intact keeps the opt-in promise intact.

## File layout

All under `config.OMM_HOME / "logs"` (`~/.omm/logs/`, honoring `OMM_HOME`):

| File | Content | Writer |
|---|---|---|
| `<ts>_<pid>_<cmd>.jsonl` | One run's full detail, one JSON object per line. Source of truth. | Only the process that owns that run. |
| `history.log` | Human-readable. One summary block per run, appended when the run ends. The file you open in an editor. | Every process, one `locked()`-guarded append at run end. |

- `<ts>` — UTC, filename-safe ISO (`2026-08-30T14-22-01Z`).
- `<pid>` — the process id, so two concurrent runs never collide.
- `<cmd>` — the resolved subcommand name (`install`, `setting`, `bare` when no
  subcommand, `unknown` when it can't be resolved). Resolved with the same
  registered-name match `_crash_subcommand()` already uses — never an echo of raw
  argv, so queries / URLs / paths don't leak into a filename.

Example jsonl line:

```json
{"ts":"2026-08-30T14:22:03.114Z","level":"INFO","event":"link","logger":"omm.linker","msg":"linked qwen2.5-7b-instruct-q4_k_m into ollama via symlink","model":"qwen2.5-7b-instruct-q4_k_m","engine":"ollama","method":"symlink"}
```

First and last lines of every jsonl file are written by the run-log machinery
itself: a `run_start` record (argv-scrubbed, version, pid, cwd) and a `run_end`
record (exit code, duration, outcome).

`history.log` block:

```
2026-08-30 14:22:01Z  omm install qwen2.5-7b-instruct-q4_k_m   (0.3.1)
  → ok  (4.7s)
  install  downloaded qwen2.5-7b-instruct-q4_k_m (4.1 GB) from huggingface
  link     qwen2.5-7b-instruct-q4_k_m → ollama (symlink)
  detail:  logs/2026-08-30T14-22-01Z_51234_install.jsonl
```

## What gets logged

### Layer 1 — every invocation (always)

Captured in `main()` around `app()`:

- `run_start`: UTC ts, omm version, pid, cwd, scrubbed argv (see Scrubbing).
- `run_end`: exit code, wall-clock duration, outcome (`ok` / `failed` /
  `usage-error` / `interrupted`).

Exit code source: `SystemExit.code` (Typer/Click raise it), `0` on clean return,
`130` on `KeyboardInterrupt`, `1` on an escaping exception.

### Layer 2 — in-command events (always, INFO)

Modules log domain events through `logging.getLogger("omm.<module>")`:

- `downloader.py` — download start / complete / fail, size, source.
- `linker.py` — link method chosen (hardlink / symlink / copy), fallbacks, ownership records.
- `registry.py` — model added / removed from `models.json`.
- `scan_import.py` — GGUFs adopted, sha256 dedup hits.
- `cli.py` uninstall / autoremove / cleanup — what was removed, what was kept and why.
- `engines/` — model load result, generation-verify pass/fail (the outcome, not the transcript).
- Any `WARNING` / `ERROR` with its context, plus tracebacks for escaping exceptions.

This is a scan-and-add pass over those modules — add `log.info(...)` at the points
that already print a user-facing line or make a state change. No behavior change.

### Layer 2 detail — `OMM_DEBUG=1` only

Gated behind the env var, off by default:

- Exact subprocess argv and exit status for external runner calls.
- HTTP request line + response status for runner API calls (never bodies, never headers).
- Full hardware-scan output, full featurize vectors.

Implementation: the file handler's level is `DEBUG` when `OMM_DEBUG` is truthy,
`INFO` otherwise. Debug call sites use `log.debug(...)`.

## Module: `omm/runlog.py`

New file. Small, no import-time side effects (no dir creation, no handler attach at
import — matches the lazy-import discipline in `cli.py`).

```python
def start(argv: list[str]) -> None:
    """Resolve the run's jsonl path, attach a JSON file handler to the
    'omm' logger, write the run_start record. Called once from main().
    Any failure here is swallowed — logging never blocks a command."""

def finish(exit_code: int, outcome: str) -> None:
    """Write run_end, flush/detach the handler, append the history.log
    block under locked(). Swallows all errors."""

def rebuild_history() -> int:
    """Regenerate history.log from every *.jsonl in the logs dir, sorted
    by ts. Returns the run count. Used by `omm log --rebuild`."""

def read_history(limit: int | None, grep: str | None) -> str:
    """Return history.log text (or a filtered slice) for `omm log`."""
```

- Reads `config.OMM_HOME` **at call time**, not at import (same binding hazard as
  `cli.SRC_DIR`; tests set `OMM_HOME` / monkeypatch `config.OMM_HOME`).
- The JSON handler is a plain `logging.FileHandler` (append mode) on the
  per-run file — no `RotatingFileHandler` (its rotation is not multiprocess-safe),
  and rotation is unnecessary because each file is one short-lived run.
- Formatter emits one `json.dumps(...)` line per record, pulling structured fields
  from `record.__dict__` extras.

## Wiring

- **`cli.main()`** — wrap the existing `try/except` around `app()`:

  ```python
  def main() -> None:
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
      except InsufficientDiskSpaceError ... :   # existing handlers, set exit_code/outcome then re-raise
          ...
      except Exception:
          exit_code, outcome = 1, "failed"
          _queue_crash_report(...)   # existing
          raise
      finally:
          runlog.finish(exit_code, outcome)
  ```

  `app()` normally exits by raising `SystemExit` even on success, so the
  `except SystemExit` branch is the common path, not the error path.

- **Domain modules** — `log = logging.getLogger(__name__)` at module top (cheap,
  no side effect), then `log.info(...)` / `log.debug(...)` at the chosen points.
  Nothing else to plumb; the handler is attached to the parent `omm` logger.

- **`omm log`** — new top-level command (read-only; small surface, acceptable as a
  verb rather than under `setting` since it is not config). `--rebuild`,
  `--lines N` (default 40), `--grep TEXT`. Prints `history.log`.

## Concurrency & failure isolation

- Per-run jsonl file: single writer, no lock needed.
- `history.log`: one append per run, wrapped in the existing
  `atomic.locked()` helper. One lock acquisition per run is negligible.
- `history.log` is derived — `omm log --rebuild` reconstructs it from the jsonl
  files if it is lost or corrupted.
- Every `runlog` entry point is wrapped so an exception (disk full, permission,
  bad path) is swallowed and the command proceeds. A logging failure must never
  change a command's exit code or output.

## Sensitive-data scrubbing

Applied before anything is written:

- **argv**: keep flags and registered subcommand names; replace any other token
  (search queries, URLs, local paths, model ids that aren't needed) — same policy
  as `_crash_subcommand`. Store the subcommand + a redacted-count, not the raw
  line. Model id is logged as its own structured field by the command when safe.
- **URLs**: strip query string and userinfo before logging (HF download URLs can
  carry `?...token=`).
- **Never logged**: `Authorization` headers, env auth tokens (`HF_TOKEN` etc.),
  API keys, request/response bodies.

## Retention

None automatic. A jsonl file is a few KB; 10k runs ≈ 30 MB. The user can delete
`logs/*.jsonl` at will and `history.log` survives (and can be rebuilt). If a size
knob is ever wanted it goes under `omm setting`, not in this pass.

## Testing

- `tests/test_runlog.py` — unit tests for `runlog` with a tmp `OMM_HOME`:
  - `start` + `finish` produce a well-formed jsonl (valid JSON per line,
    `run_start` first, `run_end` last) and one `history.log` block.
  - `getLogger("omm.x").info(...)` between `start` and `finish` lands in the jsonl.
  - `OMM_DEBUG=1` lets `log.debug` through; unset drops it.
  - argv/URL scrubbing: a query-string token and a `?token=` URL never appear.
  - `rebuild_history` reconstructs `history.log` from jsonl files in ts order.
  - A forced write failure (unwritable logs dir) is swallowed — `start`/`finish`
    return normally.
- One integration test: call `cli.main()` with monkeypatched `sys.argv`
  (`["omm", "--version"]`) and tmp `OMM_HOME`; assert a jsonl + history block exist
  with exit code 0.
- Existing suites: unaffected. `CliRunner`-based tests invoke `app()` directly, not
  `main()`, so they don't spin up the run log.

## Future (separate specs)

- Remote version/usage statistics (opt-in phone-home).
- `omm setting logs` retention/size controls, if ever needed.
