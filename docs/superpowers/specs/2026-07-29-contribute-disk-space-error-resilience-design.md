# omm contribute: disk-space handling + broad error resilience

## Problem

`omm contribute` (and other commands) crash with a raw Python traceback
whenever an uncaught exception escapes a command function. Two real cases
observed on a user's machine:

1. Disk filled up mid-download during `omm contribute` → raw
   `OSError: [Errno 28] No space left on device` from `downloader.py`,
   uncaught, kills the whole unattended session.
2. `omm autoremove` run afterward (disk still full) → the same `OSError`
   from `atomic.py`'s `atomic_write_text` while updating the link-ownership
   JSON, also uncaught.

Neither case skips gracefully; both dump Typer's Rich pretty-traceback box.

## Goals

- `omm contribute` skips a candidate that won't fit on disk *before*
  downloading it (cheap remote size check vs. free space), and moves on to
  the next candidate.
- If disk space runs out anyway mid-download (estimate was wrong, or
  another process ate the space), delete the partial file and continue
  (contribute) or fail cleanly (plain `install`) — no traceback either way.
- Every other *foreseeable* failure mode found in a full-codebase audit
  (permission errors on link/unlink, corrupt/truncated files, malformed
  network responses, etc.) gets the same treatment: caught at/near its
  source and turned into a clean skip-or-exit with a one-line message,
  following whatever handling pattern already exists for that subsystem
  (`LinkError` for linker.py, atomic-write + corrupt-backup for JSON state
  files, `QualityEvaluationError` for quality.py, etc.).
- Explicitly **not** a goal: catching everything. Anything not identified
  in the audit stays uncaught and shows the normal Rich traceback, so it
  can be reported and fixed later. No blanket `except Exception` anywhere,
  especially not in `_run_contribution_loop`.

## Design

### 1. Proactive disk-space check (`_install_impl`, cli.py)

Before starting a download (when `dest` doesn't already exist), call the
existing `remote_file_size(provider, repo_id, filename)` helper (already
used by `_pick_quant_variant`) and compare against
`shutil.disk_usage(MODELS_DIR).free`. If the size is unknown, skip the
check (fall through to the reactive path below).

- `skip_unfit=True` (contribute's fixed call, or `install --skip-unfit`):
  print a yellow "skipping — needs ~X GB, only Y GB free" note, return an
  `InstallOutcome` with a new `skipped_low_disk: bool` field (kept separate
  from `skipped_unfit`, which is about RAM/hardware fit, so the session
  summary reports the right reason).
- Otherwise (plain `install`): print a red error with the same numbers and
  `raise typer.Exit(1)`.

### 2. Reactive fallback: `InsufficientDiskSpaceError`

New `class InsufficientDiskSpaceError(DownloadError)` in `downloader.py`.
Any `OSError` with `errno == errno.ENOSPC` raised while writing a chunk
(`_download_single_stream`, `_download_range_worker`) is caught and
re-raised as this type. It's a `DownloadError` subclass so
`_is_retryable_network_error` correctly does *not* retry it, and existing
`except DownloadError` handlers upstream (`_update_one` in `omm
upgrade`) keep working unmodified.

In `_install_impl`, add a specific `except InsufficientDiskSpaceError`
branch before the general `except DownloadError` branch: call
`_cleanup_incomplete_install(filename)` to delete the partial file, then
branch the same way as case 1 (skip-and-return for `skip_unfit`, clean
`typer.Exit(1)` otherwise).

### 3. Contribution-loop bookkeeping

- `InstallOutcome.skipped_low_disk: bool = False`
- `_ContributionStats.skipped_low_disk: int = 0`
- In `_run_contribution_loop`, mirror the existing `outcome.skipped_unfit`
  block: on `outcome.skipped_low_disk`, increment the stat, `mark_seen`
  (disk space doesn't change mid-session by default, same rationale as the
  hardware-unfit case — a later session will re-check), and `continue`.
- `_print_contribution_summary` gets one more line:
  `Skipped (not enough disk space): {stats.skipped_low_disk}`.

### 4. Narrow global safety net (`main()` wrapper, cli.py + pyproject.toml)

```python
def main() -> None:
    try:
        app()
    except InsufficientDiskSpaceError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None
    except OSError as e:
        if e.errno == errno.ENOSPC:
            err_console.print(
                "[red]Not enough disk space to complete this operation. "
                "Free up space and try again.[/red]"
            )
            raise SystemExit(1) from None
        raise
```

`pyproject.toml`'s `[project.scripts]` entry changes from `omm.cli:app` to
`omm.cli:main`. `if __name__ == "__main__":` at the bottom of cli.py calls
`main()` instead of `app()`.

This catches disk-space errors from *any* code path not already handled
locally (the concrete trigger: `omm autoremove`'s registry-JSON write).
Every other exception type is re-raised untouched and still produces
Typer's normal pretty traceback — this is intentional per the goals above.

### 5. Broader hardening pass (from full-codebase audit)

Same "catch near the source, reuse the subsystem's existing error type and
handling convention" approach, applied to every gap found:

**linker.py** (install/link/unlink paths — convert unguarded `OSError` to
the existing `LinkError`, which `_link_model` already catches per-engine
and treats as "skip this engine, keep going"; unlink-side gaps become
best-effort warn-and-continue, matching `telemetry.py`/`session_cache.py`'s
existing "never raises" convention):
- `link_ollama`: `config_blob.write_bytes` (~457), `manifest_path.write_text`
  (~486), `blobs_dir.mkdir`/`manifest_dir.mkdir` (~428/479)
- `link_file`: `dst.parent.mkdir` (~275)
- `link_jan`: `config_path.write_text` (~641-645)
- `unlink_ollama`: `manifest_path.unlink` (~514) — warn+continue
- `unlink_jan`: `config_path.unlink` (~652) — warn+continue
- `read_gguf_metadata` call inside `link_ollama` (~411): catch
  `struct.error`/`KeyError` from a corrupt/truncated `.gguf` (e.g. the
  leftover of a prior disk-full failure), raise `LinkError`

**cli.py** small file-op call sites — add `try/except OSError`, exit-1 or
warn-and-continue depending on whether the op is the command's whole
purpose or one step in a loop:
- `_cleanup_incomplete_install` (~1563/1566)
- `_remove_one` (~1584) — broaden past `missing_ok=True`'s
  `FileNotFoundError`-only coverage
- `_update_one`'s `tmp.replace(dest)` (~1751)
- `_migrate_to_editable_install`'s `tmp_dir.rename(SRC_DIR)` (~715) —
  broaden existing `except FileNotFoundError` to `OSError`
- `_run_pipx_install`'s `subprocess.Popen` (~590) — same broadening
- `_autoremove_incomplete_installs` (~2389/2392)
- `omm link`'s `directory.mkdir` (~2314)

**calibration.py** write path, called from `_maybe_auto_calibrate` (auto,
after every install/contribute benchmark) and `omm setting calibrate`
(manual): wrap in `try/except OSError` at both cli.py call sites, warn,
don't block the primary result.

**providers/huggingface.py**: `resp.json()` in `fetch_repo_files` (~39) and
`remote_file_sha256` (~89) currently sit outside the existing
`try/except requests.RequestException` block. Move inside, and also catch
`ValueError` (JSON-decode failure) — mirrors `providers/modelscope.py`,
which already does this correctly. Preserves each function's documented
"return `None`/`[]` on failure" contract instead of raising.

**rules.py**: `write_text` (~48) isn't atomic like `registry.py`/`config.py`
— switch to `atomic.atomic_write_text` (root-cause fix). `load_rules()`
(~37) and `refresh_rules_with_change_note()` (~55) get the same
corrupt-file recovery pattern already used in `registry.py`/`config.py`
(`backup_corrupt_file` + fall back to a safe default) instead of letting
`json.JSONDecodeError` propagate.

**quality.py**: `write_evidence` (~1061-1065) — wrap its internal
`mkdir`/`write_text`/`replace` in `try/except OSError`, re-raise as
`QualityEvaluationError` so `benchmark_cmd`'s existing
`except quality_mod.QualityEvaluationError` handles it (currently the call
site sits outside that except's coverage — confirm and fix at the call
site too if needed).

**scan_import.py**: `adopt_group`'s `shutil.move`/`.replace` (~232, ~244) —
wrap per-group in the `_run_import_flow` loop (cli.py ~543); one group's
permission error prints a warning and moves to the next group instead of
crashing `omm import` — including the silent first-run auto-import path.

**catalog.py**: `archive_current_artifact`'s file ops (~68, ~72) — wrap in
`try/except OSError`, treat as best-effort (log/skip), don't block caching
the new model artifact.

## Explicitly out of scope

- No blanket `except Exception` in `_run_contribution_loop` or anywhere
  else — an exception type not covered by this audit is a real bug and
  should surface as a traceback so it gets reported and fixed.
- `trust/__init__.py`'s two `git` call sites that only catch
  `TimeoutExpired` (not `FileNotFoundError`) are a race-only, effectively
  unreachable gap (git's presence is already confirmed earlier in the same
  flow) — not worth touching.
- No changes to already-hardened modules: `atomic.py`, `registry.py`,
  `config.py`, `hardware.py`, `telemetry.py`, `benchmark.py`,
  `session_cache.py`, `contribute_state.py`, `benchmark_history.py`,
  `version_check.py`, `providers/modelscope.py`.

## Testing

- Unit tests for `InsufficientDiskSpaceError` conversion in downloader.py
  (mock a write raising `OSError(errno.ENOSPC, ...)`).
- Unit tests for the proactive disk-space skip in `_install_impl` (mock
  `remote_file_size` and `shutil.disk_usage`), both `skip_unfit=True` and
  `False` branches.
- Unit test for `main()`'s ENOSPC catch not swallowing other `OSError`s.
- Per-module tests for each hardening fix where the module already has a
  test file (e.g. `linker.py`, `rules.py`, `providers/huggingface.py`,
  `scan_import.py`, `catalog.py`) — simulate the failure via mock/monkeypatch
  and assert graceful handling instead of a raised exception escaping.
- Full existing test suite must stay green.
