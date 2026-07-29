# Disk-space handling + broad error resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `omm contribute` skips a model that won't fit on disk (before and during download) instead of crashing, and every other foreseeable uncaught-exception gap found in a full-codebase audit gets the same treatment - caught near its source and turned into a clean skip/exit, using whatever error-handling convention that subsystem already has.

**Architecture:** A new `InsufficientDiskSpaceError` (a `DownloadError` subclass) flows through `downloader.py` → `_install_impl` → `_run_contribution_loop`, reusing the existing `skip_unfit`-style skip-and-continue machinery. A narrow `main()` wrapper around the Typer `app()` catches only ENOSPC-flavored errors that still escape everything else. Everything else found in the audit is fixed locally: `linker.py` writes/unlinks become `LinkError` (already caught by `_link_model`) or best-effort no-ops (matching the file's own existing `except OSError: pass` idiom); `rules.py` gets an atomic write plus the same corrupt-file recovery `registry.py`/`config.py` already use; `providers/huggingface.py`'s two `resp.json()` calls move inside their existing `try` blocks; a handful of small `cli.py` file operations get `try/except OSError`.

**Tech Stack:** Python 3, Typer/Click, Rich, pytest, `pathlib`, `shutil.disk_usage`.

## Global Constraints

- No blanket `except Exception` anywhere - especially not in `_run_contribution_loop`. Only the specific exception types identified below are caught. Anything else must still surface as Typer's normal traceback.
- Every new/changed error path gets a real test with real code (mocks/monkeypatch), not a description of one.
- Full existing test suite (`pytest`) must stay green after every task.
- Match each file's existing conventions exactly: `linker.py` raises `LinkError` and does silent best-effort cleanup (`except OSError: pass`); `cli.py` prints via `console`/`err_console` and uses `typer.Exit`; `registry.py`/`config.py`'s corrupt-JSON recovery pattern (`backup_corrupt_file` + safe default) is the template for `rules.py`.
- Commit after each task, in this repo's terse style (`fix:`/`feat:` prefix, one sentence on the *why*).

---

### Task 1: `InsufficientDiskSpaceError` in `downloader.py`

**Files:**
- Modify: `src/omm/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Produces: `downloader.InsufficientDiskSpaceError` (subclass of `downloader.DownloadError`) - raised instead of a raw `OSError` whenever a chunk write hits `errno.ENOSPC`, from both the single-stream and parallel download paths. Never retried (it doesn't match `_is_retryable_network_error`).

- [ ] **Step 1: Write the failing test**

Add to the top of `tests/test_downloader.py`:

```python
import errno
import threading
from pathlib import Path

import requests

from omm import downloader
```

(replace the existing `import threading` / `import requests` / `from omm import downloader` block at the top with the above - just adding `errno` and `from pathlib import Path`).

Add this test anywhere after `_FakeResp`:

```python
def test_download_file_converts_enospc_write_error_to_insufficient_disk_space_error(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    get_calls = []
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: get_calls.append(1) or _FakeResp(200, [b"hello"])
    )

    class _FullDiskFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "open", lambda self, mode="r": _FullDiskFile())

    raised = None
    try:
        downloader.download_file("https://example.com/model.gguf", dest)
    except downloader.InsufficientDiskSpaceError as e:
        raised = e

    assert raised is not None
    assert len(get_calls) == 1  # must not be retried like a transient network error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_downloader.py::test_download_file_converts_enospc_write_error_to_insufficient_disk_space_error -v`
Expected: FAIL with `AttributeError: module 'omm.downloader' has no attribute 'InsufficientDiskSpaceError'`

- [ ] **Step 3: Implement**

In `src/omm/downloader.py`:

1. Add `import errno` to the import block (after `from __future__ import annotations`, before `import threading`).

2. Add the new exception class right after `DownloadCancelled`:

```python
class DownloadCancelled(DownloadError):
    pass


class InsufficientDiskSpaceError(DownloadError):
    pass
```

3. In `_download_single_stream`, wrap the write:

```python
        with part_path.open(mode) as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    try:
                        f.write(chunk)
                    except OSError as e:
                        if e.errno == errno.ENOSPC:
                            raise InsufficientDiskSpaceError(
                                f"Not enough disk space to download {dest.name}."
                            ) from e
                        raise
                    progress.update(task, advance=len(chunk))
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")
```

4. In `_download_range_worker`, wrap its write the same way (inside the existing outer `try`, so it still lands in `errors.append(e)`):

```python
        with part_path.open("r+b") as f:
            f.seek(start)
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                try:
                    f.write(chunk)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise InsufficientDiskSpaceError(
                            f"Not enough disk space to download {part_path.name}."
                        ) from e
                    raise
                with lock:
                    progress.update(task_id, advance=len(chunk))
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")
```

5. In `_download_parallel`, preserve the specific type instead of flattening every collected error into a generic `DownloadError`:

```python
    if errors:
        cancelled = next((e for e in errors if isinstance(e, DownloadCancelled)), None)
        if cancelled is not None:
            raise cancelled
        disk_full = next((e for e in errors if isinstance(e, InsufficientDiskSpaceError)), None)
        if disk_full is not None:
            raise disk_full
        raise DownloadError(str(errors[0])) from errors[0]
```

6. In `_attempt_download`, don't waste a doomed single-stream retry when the parallel path already proved the disk is full:

```python
def _attempt_download(
    url: str, dest: Path, part_path: Path, stop_check: Callable[[], bool] | None
) -> None:
    if not part_path.exists():
        total_size, supports_ranges = _probe_range_support(url)
        if supports_ranges and total_size >= _MIN_PARALLEL_TOTAL:
            thread_count = _choose_thread_count(total_size)
            if thread_count > 1:
                try:
                    _download_parallel(url, dest, part_path, total_size, thread_count, stop_check)
                    return
                except (DownloadCancelled, InsufficientDiskSpaceError):
                    raise
                except DownloadError:
                    part_path.unlink(missing_ok=True)
                    # fall through to a clean single-stream retry

    _download_single_stream(url, dest, part_path, stop_check)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: PASS (all tests in the file, not just the new one)

- [ ] **Step 5: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "$(cat <<'EOF'
feat: raise InsufficientDiskSpaceError instead of raw OSError on ENOSPC

A full disk during download previously surfaced as an uncaught raw
OSError with a Python traceback. This gives it a distinct DownloadError
subclass callers can catch and skip on, matching how network errors are
already handled.
EOF
)"
```

---

### Task 2: Proactive + reactive disk-space handling in `_install_impl` (cli.py)

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_install_impl.py`

**Interfaces:**
- Consumes: `downloader.InsufficientDiskSpaceError` (Task 1); `hub.remote_file_size(provider, repo_id, filename) -> int | None` (already exists); `shutil.disk_usage(path).free` (stdlib).
- Produces: `InstallOutcome.skipped_low_disk: bool = False` - a new field, parallel to the existing `skipped_unfit`, that later tasks (3) read.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_install_impl.py` (it already has `from types import SimpleNamespace` and `import pytest` at the top - no new imports needed):

```python
def test_install_impl_exits_when_not_enough_disk_space_and_not_skip_unfit(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 50 * 1024**3)
    monkeypatch.setattr(
        cli.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3)
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: download_calls.append(a))

    with pytest.raises(cli.typer.Exit) as exc_info:
        cli._install_impl(_resolved())

    assert exc_info.value.exit_code == 1
    assert download_calls == []


def test_install_impl_skips_gracefully_when_not_enough_disk_space_and_skip_unfit(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 50 * 1024**3)
    monkeypatch.setattr(
        cli.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3)
    )
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda *a, **k: download_calls.append(a))

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    assert download_calls == []


def test_install_impl_proceeds_when_disk_space_check_is_inconclusive(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 10.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved())

    assert outcome.tokens_per_sec == 10.0


def test_install_impl_cleans_up_partial_file_and_skips_on_insufficient_disk_space_mid_download(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)

    def fake_download(url, dest, **kwargs):
        dest.with_suffix(dest.suffix + ".part").write_bytes(b"partial")
        raise cli.InsufficientDiskSpaceError("disk full mid-download")

    monkeypatch.setattr(cli, "download_file", fake_download)

    outcome = cli._install_impl(_resolved(), skip_unfit=True)

    assert outcome.skipped_low_disk is True
    dest = cli.MODELS_DIR / "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_install_impl_exits_cleanly_on_insufficient_disk_space_mid_download_without_skip_unfit(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)
    monkeypatch.setattr(
        cli,
        "download_file",
        lambda *a, **k: (_ for _ in ()).throw(cli.InsufficientDiskSpaceError("disk full")),
    )

    with pytest.raises(cli.typer.Exit) as exc_info:
        cli._install_impl(_resolved())

    assert exc_info.value.exit_code == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_install_impl.py -k disk_space -v`
Expected: FAIL (`AttributeError: 'module' object has no attribute 'InsufficientDiskSpaceError'` and/or `skipped_low_disk` not a field of `InstallOutcome`, and/or no exit raised)

- [ ] **Step 3: Implement**

In `src/omm/cli.py`:

1. Add `InsufficientDiskSpaceError` to the downloader import (line ~56):

```python
from omm.downloader import DownloadCancelled, DownloadError, InsufficientDiskSpaceError, download_file
```

2. Add the new field to `InstallOutcome` (right after `skipped_unfit: bool = False`, ~line 1174):

```python
@dataclass
class InstallOutcome:
    filename: str
    repo_id: str | None
    linked: dict[str, bool]
    ollama_tag: str | None = None
    tokens_per_sec: float | None = None
    telemetry_sent: bool = False
    skipped_unfit: bool = False
    skipped_low_disk: bool = False
    sha256: str | None = None
    failure_reason: str | None = None
    model_metadata: dict | None = None
```

3. Replace the download block in `_install_impl` (currently ~lines 1299-1312):

```python
    dest = MODELS_DIR / filename
    if dest.exists():
        err_console.print(f"[yellow]{filename} already downloaded, skipping fetch.[/yellow]")
    else:
        size_bytes = remote_file_size(resolved.provider or "huggingface", repo_id, filename) if repo_id else None
        if size_bytes:
            free_gb = shutil.disk_usage(MODELS_DIR).free / (1024**3)
            required_gb = size_bytes / (1024**3)
            if required_gb > free_gb:
                message = (
                    f"{filename} needs ~{required_gb:.1f}GB but only "
                    f"{free_gb:.1f}GB free on disk"
                )
                if skip_unfit:
                    err_console.print(f"[yellow]Skipping {message}.[/yellow]")
                    return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
                err_console.print(f"[red]Not enough disk space: {message}.[/red]")
                raise typer.Exit(1)
        try:
            if stop_event is not None:
                download_file(url, dest, stop_check=stop_event.is_set)
            else:
                download_file(url, dest)
        except DownloadCancelled as e:
            raise ContributionStopped(filename) from e
        except InsufficientDiskSpaceError as e:
            _cleanup_incomplete_install(filename)
            err_console.print(f"[red]{e}[/red]")
            if skip_unfit:
                return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
            raise typer.Exit(1) from e
        except DownloadError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
```

Note `shutil` and `remote_file_size` are already imported at module level (`import shutil` near the top; `remote_file_size` in the `from omm.hub import (...)` block) - no new imports needed for this step. `_cleanup_incomplete_install` is defined later in the file but Python resolves it at call time, so forward reference is fine (matches how the rest of the file already calls it from `_run_contribution_loop`, which is defined even earlier).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_install_impl.py -v`
Expected: PASS (all tests, including the 5 new ones and every pre-existing one - `remote_file_size`/`shutil.disk_usage` are unmocked real calls in pre-existing tests, so confirm none of them hit the network; if any pre-existing test's `_resolved()` has a `repo_id`, `remote_file_size` will actually be called for real HTTP - check for that and mock it to return `None` in any pre-existing test that now fails for this reason).

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "$(cat <<'EOF'
feat: skip a model that won't fit on disk instead of downloading and failing

_install_impl now checks the model's known remote size against free disk
space before starting a download, and cleans up + skips (contribute) or
exits cleanly (plain install) if InsufficientDiskSpaceError still happens
mid-download because the estimate was off or another process ate the
space.
EOF
)"
```

---

### Task 3: `_run_contribution_loop` bookkeeping for `skipped_low_disk`

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_contribute_loop.py`

**Interfaces:**
- Consumes: `InstallOutcome.skipped_low_disk` (Task 2).
- Produces: `_ContributionStats.skipped_low_disk: int = 0`; one more line in `_print_contribution_summary`'s output.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_contribute_loop.py`:

```python
def test_skipped_low_disk_candidate_counted_and_not_deleted(isolated_omm_home, monkeypatch):
    c = _candidate(filename="too-big.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="too-big.gguf", repo_id="org/repo", linked={}, skipped_low_disk=True
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.skipped_low_disk == 1
    assert stats.benchmarked == []
    assert removed == []
    assert queue.marked_seen == ["huggingface:org/repo:too-big.gguf"]


def test_print_contribution_summary_includes_low_disk_skip_count(capsys):
    stats = cli._ContributionStats(benchmarked=[], skipped_unfit=1, skipped_low_disk=2)

    cli._print_contribution_summary(stats, 12.0, None, None)

    captured = capsys.readouterr()
    assert "not enough disk space): 2" in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_contribute_loop.py -k "low_disk" -v`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'skipped_low_disk'`, and the summary line missing)

- [ ] **Step 3: Implement**

In `src/omm/cli.py`:

1. Add the field to `_ContributionStats` (~line 2924):

```python
@dataclass
class _ContributionStats:
    benchmarked: list[tuple[str, float]]
    skipped_unfit: int = 0
    skipped_low_disk: int = 0
    attempted_not_uploaded: int = 0
    daemon_restarts: int = 0
    given_up_on: int = 0
    exhausted: bool = False
```

2. In `_run_contribution_loop`, right after the existing `if outcome.skipped_unfit:` block (~line 3122-3133), add a mirrored block:

```python
        if outcome.skipped_unfit:
            stats.skipped_unfit += 1
            queue.mark_seen(ref_str)
            continue

        if outcome.skipped_low_disk:
            stats.skipped_low_disk += 1
            # Free space doesn't reliably change mid-session either, and a
            # later `omm contribute` run will re-check it from scratch -
            # same rationale as the skipped_unfit case just above.
            queue.mark_seen(ref_str)
            continue
```

(Keep the existing comment already attached to `skipped_unfit`'s `mark_seen` call as-is; only add the new block below it.)

3. In `_print_contribution_summary`, add one line after the existing `skipped_unfit` line:

```python
    console.print(f"Skipped (predicted not to fit this hardware): {stats.skipped_unfit}")
    console.print(f"Skipped (not enough disk space): {stats.skipped_low_disk}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_contribute_loop.py tests/test_cli_contribute.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_contribute_loop.py
git commit -m "$(cat <<'EOF'
feat: track and report disk-space skips in omm contribute's session summary
EOF
)"
```

---

### Task 4: Narrow global ENOSPC safety net (`main()` wrapper)

**Files:**
- Modify: `src/omm/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_cli_main_entrypoint.py` (new)

**Interfaces:**
- Produces: `cli.main() -> None` - the new console-script entry point. Calls `app()`; catches `InsufficientDiskSpaceError` and `OSError(errno=ENOSPC)` only, printing one line and exiting 1. Everything else re-raises unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_main_entrypoint.py`:

```python
import errno

import pytest

from omm import cli


def test_main_prints_friendly_message_on_enospc_oserror_and_exits_1(monkeypatch, capsys):
    def _raise_enospc():
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cli, "app", _raise_enospc)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "disk space" in captured.err.lower()


def test_main_prints_friendly_message_on_insufficient_disk_space_error(monkeypatch, capsys):
    def _raise_disk_space_error():
        raise cli.InsufficientDiskSpaceError("model.gguf needs 5.0GB but only 1.0GB free")

    monkeypatch.setattr(cli, "app", _raise_disk_space_error)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "5.0GB" in captured.err


def test_main_reraises_non_enospc_oserror_unchanged(monkeypatch):
    def _raise_permission_denied():
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(cli, "app", _raise_permission_denied)

    with pytest.raises(OSError) as exc_info:
        cli.main()
    assert exc_info.value.errno == errno.EACCES


def test_main_reraises_other_exceptions_unchanged(monkeypatch):
    def _raise_value_error():
        raise ValueError("some genuine bug")

    monkeypatch.setattr(cli, "app", _raise_value_error)

    with pytest.raises(ValueError):
        cli.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_main_entrypoint.py -v`
Expected: FAIL with `AttributeError: module 'omm.cli' has no attribute 'main'`

- [ ] **Step 3: Implement**

In `src/omm/cli.py`:

1. Add `import errno` to the top import block (before `import json`, keeping the existing alphabetical order: `errno, json, math, platform, re, shutil, subprocess, sys, threading, time`).

2. Replace the very end of the file (currently):

```python
if __name__ == "__main__":
    app()
```

with:

```python
def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts]).
    Catches disk-full errors that escape every local handler - e.g. a JSON
    write during `omm autoremove` - and prints one clean line instead of
    Typer's default traceback. Everything else propagates untouched so a
    genuine bug still surfaces as a normal traceback and can be reported."""
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


if __name__ == "__main__":
    main()
```

3. In `pyproject.toml`, change:

```toml
[project.scripts]
omm = "omm.cli:app"
```

to:

```toml
[project.scripts]
omm = "omm.cli:main"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_main_entrypoint.py -v`
Expected: PASS

Then run the full suite to make sure nothing else assumed the old entry point: `python -m pytest -q`

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py pyproject.toml tests/test_cli_main_entrypoint.py
git commit -m "$(cat <<'EOF'
fix: catch disk-full errors at the entry point instead of a raw traceback

omm autoremove (and any other command that writes JSON state when the
disk is completely full) previously crashed with Typer's default
Rich-boxed traceback. main() now catches only ENOSPC-flavored errors
that still reach the top - anything else still shows a normal traceback.
EOF
)"
```

---

### Task 5: `linker.py` - convert unguarded `OSError` to `LinkError` / best-effort no-ops

**Files:**
- Modify: `src/omm/linker.py`
- Test: `tests/test_linker_link_file.py`, `tests/test_linker_new_engines.py`, `tests/test_linker_autoremove.py`

**Interfaces:**
- No new public names. `link_ollama`, `link_file`, `link_jan` now raise `LinkError` (already-existing type, already caught by `cli.py`'s `_link_model`) instead of a raw `OSError` on a write/mkdir failure, and instead of `struct.error`/`KeyError` on a corrupt `.gguf`. `unlink_ollama`, `unlink_jan`, and the unlink step inside `autoremove_ollama`/`autoremove_jan` now silently skip (return without raising) on a permission error, matching the file's existing `except OSError: pass` idiom for the adjacent `rmdir()` calls.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_linker_link_file.py` (add `import struct` to its imports, alongside the existing `from pathlib import Path` / `import pytest` / `from omm import linker`):

```python
def test_link_file_raises_link_error_when_mkdir_fails(tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError("permission denied")))

    with pytest.raises(linker.LinkError):
        linker.link_file(src, dst)


def test_link_ollama_raises_link_error_when_blob_write_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    monkeypatch.setattr(
        Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(linker.LinkError):
        linker.link_ollama(source, "model", models_dir=models_dir)


def test_link_ollama_raises_link_error_on_corrupt_gguf_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"

    def _raise_struct_error(*a, **k):
        raise struct.error("unpack requires a buffer of 8 bytes")

    monkeypatch.setattr(linker, "read_gguf_metadata", _raise_struct_error)

    with pytest.raises(linker.LinkError, match="corrupted"):
        linker.link_ollama(source, "model", models_dir=models_dir)
```

Add to `tests/test_linker_new_engines.py` (it already imports `linker` and `pytest`; add `from pathlib import Path` too):

```python
def test_link_jan_raises_link_error_when_write_fails(tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"weights")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan")
    monkeypatch.setattr(
        Path, "write_text", lambda self, content: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(linker.LinkError):
        linker.link_jan(gguf_path, "model-id")


def test_unlink_ollama_swallows_permission_error(tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    linker.link_ollama(source, "model", models_dir=models_dir)

    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    linker.unlink_ollama("model", models_dir=models_dir)  # must not raise


def test_unlink_jan_swallows_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan")
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"weights")
    linker.link_jan(gguf_path, "model-id")

    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    linker.unlink_jan("model-id")  # must not raise


def test_autoremove_jan_skips_manifest_it_cannot_unlink(tmp_path, monkeypatch):
    models_dir = tmp_path / "jan"
    model_dir = models_dir / "model-id"
    model_dir.mkdir(parents=True)
    config_path = model_dir / "model.yml"
    config_path.write_text('model_path: "/gone/model.gguf"\n')
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    removed = linker.autoremove_jan()  # must not raise

    assert removed == 0
    assert config_path.exists()
```

Add to `tests/test_linker_autoremove.py` (add `from pathlib import Path` alongside its existing `import json` / `import pytest` / `from omm import linker`):

```python
def test_autoremove_ollama_skips_manifest_it_cannot_unlink(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)

    broken_digest_hex = "a" * 64
    broken_blob = blobs_dir / f"sha256-{broken_digest_hex}"
    try:
        broken_blob.symlink_to(tmp_path / "gone.gguf")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation on this Windows host")
    linker._record_symlink(broken_blob, tmp_path / "gone.gguf")

    manifests_root = models_dir / "manifests" / "registry.ollama.ai" / "library"
    broken_manifest_dir = manifests_root / "broken-model"
    broken_manifest_dir.mkdir(parents=True)
    broken_manifest = broken_manifest_dir / "latest"
    broken_manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{broken_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{broken_digest_hex}", "size": 1}],
            }
        )
    )
    linker._record_ownership(broken_manifest, None, "manifest")

    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)
    real_unlink = Path.unlink

    def _flaky_unlink(self, missing_ok=False):
        if self == broken_manifest:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    blobs_removed, manifests_removed = linker.autoremove_ollama()  # must not raise

    assert blobs_removed == 1
    assert manifests_removed == 0
    assert broken_manifest.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_linker_link_file.py tests/test_linker_new_engines.py tests/test_linker_autoremove.py -v`
Expected: FAIL on every new test (raw `OSError`/`struct.error` propagates instead of `LinkError`; the "swallow" tests raise instead of returning cleanly).

- [ ] **Step 3: Implement**

In `src/omm/linker.py`:

1. Add `import struct` to the imports (alongside `import hashlib`, `import json`, `import os`, `import platform`, `import re`, `import shutil`).

2. `link_file` - wrap the `mkdir` (currently the first line of the function body):

```python
def link_file(src: Path, dst: Path) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise LinkError(f"Could not create directory {dst.parent}: {e}") from e
    if dst.exists() or dst.is_symlink():
        ...
```

(everything below `if dst.exists()...` is unchanged.)

3. `link_ollama` - wrap the metadata read separately from the write section:

```python
    gguf_meta = read_gguf_metadata(gguf_path, {"general.architecture", "tokenizer.chat_template"})
```

becomes:

```python
    try:
        gguf_meta = read_gguf_metadata(gguf_path, {"general.architecture", "tokenizer.chat_template"})
    except (struct.error, KeyError) as e:
        raise LinkError(
            f"Could not read GGUF metadata from {gguf_path.name}: corrupted or truncated file ({e})."
        ) from e
```

Then wrap everything from `blobs_dir = models_dir / "blobs"` through the end of the existing `_record_ownership` try/except, ending right before `return has_chat_template`, in one outer `try/except OSError`. Concretely, change:

```python
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    model_blob = blobs_dir / f"sha256-{model_sha256}"
    ...
    manifest_path.write_text(json.dumps(manifest, indent=2))
    try:
        _record_ownership(manifest_path, None, "manifest")
    except OSError:
        manifest_path.unlink(missing_ok=True)
        raise

    return has_chat_template
```

to:

```python
    blobs_dir = models_dir / "blobs"
    try:
        blobs_dir.mkdir(parents=True, exist_ok=True)

        model_blob = blobs_dir / f"sha256-{model_sha256}"
        # A matching content-addressed blob may be owned by Ollama or another
        # manifest. It is already usable; never replace it.
        if not model_blob.exists() and not model_blob.is_symlink():
            link_file(gguf_path, model_blob)

        config = {
            "model_format": "gguf",
            "model_family": architecture,
            "model_families": [architecture],
            "model_type": _guess_param_size(gguf_path.name),
            "file_type": _guess_quant(gguf_path.name),
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [model_digest]},
        }
        config_bytes = json.dumps(config).encode()
        config_sha256 = hashlib.sha256(config_bytes).hexdigest()
        config_blob = blobs_dir / f"sha256-{config_sha256}"
        if not config_blob.exists() and not config_blob.is_symlink():
            config_blob.write_bytes(config_bytes)

        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": f"sha256:{config_sha256}",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.ollama.image.model",
                    "digest": model_digest,
                    "size": gguf_path.stat().st_size,
                }
            ],
        }

        manifest_dir = (
            models_dir / "manifests" / "registry.ollama.ai" / "library" / model_name
        )
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "latest"
        if manifest_path.exists() or manifest_path.is_symlink():
            if not _owned_manifest(manifest_path):
                raise LinkError(f"Refusing to replace unowned Ollama manifest at {manifest_path}.")
            manifest_path.unlink()
            _update_link_ownership(manifest_path, None)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        try:
            _record_ownership(manifest_path, None, "manifest")
        except OSError:
            manifest_path.unlink(missing_ok=True)
            raise
    except OSError as e:
        raise LinkError(f"Could not link {model_name} into Ollama: {e}") from e

    return has_chat_template
```

(This is a pure re-indent of the existing body under one `try:`, plus the new `except OSError as e: raise LinkError(...) from e` at the end - no logic changes. `link_file(gguf_path, model_blob)` can itself raise `LinkError`, which is *not* an `OSError`, so it passes through this new wrapper untouched, exactly as before.)

4. `link_jan` - wrap its body:

```python
def link_jan(gguf_path: Path, model_id: str) -> Path:
    """Register `gguf_path` with Jan by writing a model.yml manifest that
    points model_path straight at it - no symlink needed, since Jan's own
    local-file import does the same (stores the absolute path as-is)."""
    config_path = _jan_model_yaml_path(model_id)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            f'model_path: "{gguf_path}"\n'
            f'name: "{model_id}"\n'
            f"size_bytes: {gguf_path.stat().st_size}\n"
        )
    except OSError as e:
        raise LinkError(f"Could not write Jan manifest at {config_path}: {e}") from e
    return config_path
```

5. `unlink_ollama` - swallow a permission error on the unlink itself, matching the existing `except OSError: pass` right below it:

```python
def unlink_ollama(model_name: str, models_dir: Path | None = None) -> None:
    if models_dir is None:
        models_dir = ollama_models_dir()
    manifest_path = (
        models_dir
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / model_name
        / "latest"
    )
    if not manifest_path.exists():
        return
    if not _owned_manifest(manifest_path):
        return
    # Blobs are content-addressed and can be shared by a user manifest or
    # another omm model. Leave their collection to Ollama; only our manifest
    # is safe to remove here.
    try:
        manifest_path.unlink()
    except OSError:
        return
    _update_link_ownership(manifest_path, None)
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass
```

6. `unlink_jan` - same treatment:

```python
def unlink_jan(model_id: str) -> None:
    config_path = _jan_model_yaml_path(model_id)
    if config_path.exists():
        try:
            config_path.unlink()
        except OSError:
            return
    try:
        config_path.parent.rmdir()
    except OSError:
        pass
```

7. `autoremove_ollama` - wrap the manifest unlink inside its loop:

```python
    manifests_removed = 0
    if broken_digests and manifests_root.exists():
        for manifest_path in list(manifests_root.rglob("latest")):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            layer_digests = {
                layer["digest"].replace(":", "-") for layer in manifest.get("layers", [])
            }
            if layer_digests & broken_digests and _owned_manifest(manifest_path):
                try:
                    manifest_path.unlink()
                except OSError:
                    continue
                _update_link_ownership(manifest_path, None)
                manifests_removed += 1
                try:
                    manifest_path.parent.rmdir()
                except OSError:
                    pass
```

8. `autoremove_jan` - wrap its unlink:

```python
def autoremove_jan() -> int:
    """Delete model.yml manifests whose model_path no longer points at an
    existing file. Returns the number removed."""
    models_dir = jan_models_dir()
    if not models_dir.exists():
        return 0
    removed = 0
    for config_path in list(models_dir.glob("*/model.yml")):
        model_path = read_jan_model_path(config_path)
        if model_path and not Path(model_path).exists():
            try:
                config_path.unlink()
            except OSError:
                continue
            removed += 1
            try:
                config_path.parent.rmdir()
            except OSError:
                pass
    return removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_linker_link_file.py tests/test_linker_new_engines.py tests/test_linker_autoremove.py -v`
Expected: PASS

Then: `python -m pytest -q` (full suite - `link_ollama`'s reindent is the highest-risk change here for accidentally breaking an existing test).

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_link_file.py tests/test_linker_new_engines.py tests/test_linker_autoremove.py
git commit -m "$(cat <<'EOF'
fix: convert unguarded OSError in linker.py to LinkError or a no-op

Permission errors while writing an Ollama/Jan manifest or blob, and a
corrupted/truncated .gguf's metadata read, previously crashed with a raw
traceback. Writes now raise the existing LinkError (already caught by
_link_model, which skips just that one engine); best-effort unlinks
during uninstall/autoremove now swallow a permission error the same way
the adjacent rmdir() calls already do.
EOF
)"
```

---

### Task 6: Calibration write failures don't block install/benchmark

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_install_impl.py`, `tests/test_cli_calibrate_local.py`

**Interfaces:**
- No new names. `_maybe_auto_calibrate` and the `omm setting calibrate` command both catch `OSError` around `calibration.record_calibration(...)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_install_impl.py`:

```python
def test_auto_calibrate_does_not_crash_install_when_write_fails(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]}
    )
    hw_stub = SimpleNamespace(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16.0,
        ram_available_gb=12.0,
        vram_total_gb=None,
        vram_free_gb=None,
        unified_memory=False,
        gpu_name=None,
        gpu_tflops=None,
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: hw_stub)
    monkeypatch.setattr(
        cli.predictor, "predict_speed_interval", lambda *args, **kwargs: (20.0, 20.0, 20.0)
    )
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = cli._install_impl(_resolved())  # must not raise

    assert outcome.tokens_per_sec == 30.0
```

Add to `tests/test_cli_calibrate_local.py`:

```python
def test_calibrate_reports_clean_error_when_write_fails(isolated_omm_home, monkeypatch):
    filename = "model-1B-Q4.gguf"
    registry.save_registry(
        {
            filename: {
                "repo_id": "org/model",
                "size_bytes": 1024,
                "ollama_name": "model",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]})
    monkeypatch.setattr(
        cli.predictor, "predict_speed_interval", lambda *args, **kwargs: (20.0, 20.0, 20.0)
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = runner.invoke(cli.app, ["setting", "calibrate", filename])

    assert result.exit_code == 1
    assert "disk full" in result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_install_impl.py::test_auto_calibrate_does_not_crash_install_when_write_fails tests/test_cli_calibrate_local.py::test_calibrate_reports_clean_error_when_write_fails -v`
Expected: FAIL - `OSError: disk full` propagates uncaught in both.

- [ ] **Step 3: Implement**

In `src/omm/cli.py`, `_maybe_auto_calibrate` (~line 1272-1281), wrap the write:

```python
    try:
        factor = calibration.record_calibration(
            hardware,
            measured_tokens_per_sec=tokens_per_sec,
            predicted_tokens_per_sec=predicted,
            engine="ollama",
        )
    except OSError:
        return
    console.print(
        f"[dim]Local calibration updated: correction ×{factor:.2f} "
        "(not uploaded).[/dim]"
    )
```

In the `calibrate` command (`omm setting calibrate`, ~line 2035-2045), wrap the write:

```python
    try:
        factor = calibration.record_calibration(
            hardware,
            measured_tokens_per_sec=measured,
            predicted_tokens_per_sec=predicted,
            engine="ollama",
        )
    except OSError as e:
        err_console.print(f"[red]Could not save calibration: {e}[/red]")
        raise typer.Exit(1) from e
    console.print(
        f"[green]Local calibration saved: {measured:.1f} tok/s measured, "
        f"{predicted:.1f} predicted, correction ×{factor:.2f}.[/green]"
    )
    console.print("[dim]The calibration stays in ~/.omm and was not uploaded.[/dim]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_install_impl.py tests/test_cli_calibrate_local.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py tests/test_cli_calibrate_local.py
git commit -m "$(cat <<'EOF'
fix: don't let a calibration-file write failure crash install or calibrate

Auto-calibration after a benchmark is a best-effort side effect and must
never take the install down with it; the explicit `omm setting calibrate`
command still reports the failure since saving is its entire purpose.
EOF
)"
```

---

### Task 7: `providers/huggingface.py` - guard malformed JSON responses

**Files:**
- Modify: `src/omm/providers/huggingface.py`
- Test: `tests/test_provider_huggingface.py` (new)

**Interfaces:**
- No new names. `fetch_repo_files` and `remote_file_sha256` keep their documented contracts (`fetch_repo_files` raises `ModelResolutionError` on failure; `remote_file_sha256` returns `None`) instead of leaking a raw `ValueError`/`KeyError` on a malformed response body.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_huggingface.py`:

```python
import requests

from omm.providers import huggingface
from omm.providers.base import ModelResolutionError


class _FakeResponse:
    def __init__(self, *, json_error=None, payload=None):
        self._json_error = json_error
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def test_fetch_repo_files_raises_model_resolution_error_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, timeout: _FakeResponse(json_error=ValueError("bad json"))
    )

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_fetch_repo_files_raises_model_resolution_error_on_missing_key(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout: _FakeResponse(payload={"siblings": [{"unexpected": "shape"}]}),
    )

    try:
        huggingface.fetch_repo_files("org/repo")
        assert False, "expected ModelResolutionError"
    except ModelResolutionError:
        pass


def test_remote_file_sha256_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda url, json, timeout: _FakeResponse(json_error=ValueError("bad json"))
    )

    assert huggingface.remote_file_sha256("org/repo", "model.gguf") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_huggingface.py -v`
Expected: FAIL - `ValueError`/`KeyError` propagates uncaught instead of the documented return/exception contract.

- [ ] **Step 3: Implement**

In `src/omm/providers/huggingface.py`, `fetch_repo_files`, replace:

```python
    payload = resp.json()
    siblings = payload.get("siblings", [])
    files = [s["rfilename"] for s in siblings if s["rfilename"].endswith(".gguf")]
    param_count_b = _parse_gguf_total_params(payload)
    return files, param_count_b
```

with:

```python
    try:
        payload = resp.json()
        siblings = payload.get("siblings", [])
        files = [s["rfilename"] for s in siblings if s["rfilename"].endswith(".gguf")]
        param_count_b = _parse_gguf_total_params(payload)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ModelResolutionError(f"HF API returned an unexpected response for '{repo_id}': {e}") from e
    return files, param_count_b
```

In `remote_file_sha256`, replace:

```python
    entries = resp.json()
    if not entries:
        return None
    return entries[0].get("lfs", {}).get("sha256")
```

with:

```python
    try:
        entries = resp.json()
        if not entries:
            return None
        return entries[0].get("lfs", {}).get("sha256")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_huggingface.py -v`
Expected: PASS

Then: `python -m pytest tests/test_hub.py tests/test_hub_remote_sha256.py tests/test_hub_multi_provider.py -v` (regression check on the existing happy-path callers of these two functions)

- [ ] **Step 5: Commit**

```bash
git add src/omm/providers/huggingface.py tests/test_provider_huggingface.py
git commit -m "$(cat <<'EOF'
fix: guard HF API JSON parsing against malformed responses

A captive portal, proxy error page, or HF API shape change previously
raised a raw ValueError/KeyError past these functions' documented
failure contracts (raise ModelResolutionError / return None).
EOF
)"
```

---

### Task 8: `rules.py` - atomic write + corrupt-file recovery

**Files:**
- Modify: `src/omm/rules.py`
- Test: `tests/test_rules_change_note.py`

**Interfaces:**
- Produces: `rules._read_rules_file() -> list[dict] | None` - new private helper, used by both `load_rules()` and `refresh_rules_with_change_note()`. Returns `None` if the file doesn't exist or can't be parsed (after backing it up via `atomic.backup_corrupt_file`), instead of letting `json.JSONDecodeError` propagate.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rules_change_note.py`:

```python
def test_load_rules_falls_back_to_defaults_on_corrupt_file(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text("{not valid json")
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)

    rules = rules_mod.load_rules()

    assert rules == rules_mod.DEFAULT_RULES
    backups = list(tmp_path.glob("rules.json.corrupt-*"))
    assert len(backups) == 1


def test_refresh_rules_with_change_note_treats_corrupt_previous_file_as_no_prior_data(
    monkeypatch, tmp_path
):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text("{not valid json")
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [{"name": "a"}])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is True
    assert fetched == [{"name": "a"}]


def test_fetch_rules_writes_atomically(monkeypatch, tmp_path):
    """A crash/interruption mid-write must never leave a corrupt rules.json -
    fetch_rules should go through atomic_write_text like registry/config do,
    not a plain write_text."""
    rules_path = tmp_path / "rules.json"
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    write_calls = []
    original_atomic_write_text = rules_mod.atomic_write_text

    def spy(path, content):
        write_calls.append(path)
        return original_atomic_write_text(path, content)

    monkeypatch.setattr(rules_mod, "atomic_write_text", spy)

    import requests

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"name": "a"}]

    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse())

    rules_mod.fetch_rules("http://example.com/rules.json")

    assert write_calls == [rules_path]
    assert rules_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rules_change_note.py -v`
Expected: FAIL - `load_rules`/`refresh_rules_with_change_note` raise `json.JSONDecodeError` on the corrupt file; `atomic_write_text` (not yet imported/used in `rules.py`) has no `spy` to intercept.

- [ ] **Step 3: Implement**

Replace `src/omm/rules.py`'s body from the `load_rules` function through `refresh_rules_with_change_note` with:

```python
from omm.atomic import atomic_write_text, backup_corrupt_file
from omm.config import RULES_PATH

DEFAULT_RULES: list[dict] = [
    ...  # unchanged
]


def _read_rules_file() -> list[dict] | None:
    if not RULES_PATH.exists():
        return None
    try:
        return json.loads(RULES_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        backup_corrupt_file(RULES_PATH)
        return None


def load_rules() -> list[dict]:
    rules = _read_rules_file()
    return rules if rules is not None else DEFAULT_RULES


def fetch_rules(url: str) -> list[dict]:
    import requests

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    rules = resp.json()
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(RULES_PATH, json.dumps(rules, indent=2))
    return rules


def refresh_rules_with_change_note(url: str) -> tuple[list[dict], bool]:
    """Like fetch_rules, but also reports whether the fetched rules differ
    from what was already cached."""
    previous = _read_rules_file()
    fetched = fetch_rules(url)
    return fetched, fetched != previous
```

(Only the import line and the four function definitions change; `DEFAULT_RULES` and `matching_rules` stay exactly as they are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rules_change_note.py -v`
Expected: PASS

Then check `omm recommend`'s rules-fallback path still works: `python -m pytest -k recommend -q`

- [ ] **Step 5: Commit**

```bash
git add src/omm/rules.py tests/test_rules_change_note.py
git commit -m "$(cat <<'EOF'
fix: make rules.json writes atomic and reads corruption-tolerant

fetch_rules used a plain write_text (unlike registry.py/config.py, which
already write atomically) - a crash mid-write could leave rules.json
truncated, and the next read had no recovery path at all. Now matches
the existing atomic-write + backup-and-fall-back-to-defaults pattern.
EOF
)"
```

---

### Task 9: `catalog.py` - `archive_current_artifact` is best-effort

**Files:**
- Modify: `src/omm/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- No new names. `archive_current_artifact` already returns `Path | None`; on `OSError` it now returns `None` (same as its existing "nothing to archive" case) instead of raising.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_catalog.py` (add `import errno` and `from pathlib import Path` to its imports):

```python
def test_archive_current_artifact_returns_none_on_write_failure(tmp_path, monkeypatch):
    artifact = tmp_path / "recommend.json"
    artifact.write_text('{"version":1}')
    history = tmp_path / "history"

    monkeypatch.setattr(
        Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
    )

    assert catalog.archive_current_artifact(artifact, history) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_catalog.py::test_archive_current_artifact_returns_none_on_write_failure -v`
Expected: FAIL - `OSError` propagates uncaught.

- [ ] **Step 3: Implement**

In `src/omm/catalog.py`, replace `archive_current_artifact`:

```python
def archive_current_artifact(
    artifact_path: Path | None = None,
    history_dir: Path | None = None,
) -> Path | None:
    source = artifact_path or RECOMMEND_MODEL_PATH
    destination_dir = history_dir or CATALOG_HISTORY_DIR
    if not source.exists():
        return None
    try:
        content = source.read_bytes()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{sha256_bytes(content)}.json"
        if not destination.exists():
            destination.write_bytes(content)
    except OSError:
        return None
    return destination
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_catalog.py -v`
Expected: PASS (including the pre-existing `test_catalog_rollback_restores_previous_different_snapshot`, which calls `archive_current_artifact` on the happy path and discards its return value either way - unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/omm/catalog.py tests/test_catalog.py
git commit -m "$(cat <<'EOF'
fix: treat a failed catalog history archive as best-effort, not fatal

archive_current_artifact's only two callers already discard its return
value - a permission/disk error while archiving a rollback snapshot must
not block caching the new recommendation model or performing a rollback.
EOF
)"
```

---

### Task 10: `omm import` continues after one group fails

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_cli_import.py`

**Interfaces:**
- No new names. `_run_import_flow`'s per-group loop catches `(OSError, linker.LinkError)` around `scan_import.adopt_group(group)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_import.py`:

```python
def test_import_continues_after_one_group_fails(isolated_omm_home, monkeypatch):
    failing_group = _fake_group(sha256="baadf00d")
    ok_group = _fake_group(sha256="deadbeef")
    monkeypatch.setattr(
        cli.scan_import, "find_external_models", lambda extra_path=None: [object(), object()]
    )
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [failing_group, ok_group])

    adopted = []

    def fake_adopt(g):
        if g.sha256 == "baadf00d":
            raise OSError("disk full")
        adopted.append(g.sha256)
        return SimpleNamespace(filename="model.gguf", bytes_saved=0)

    monkeypatch.setattr(cli.scan_import, "adopt_group", fake_adopt)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert adopted == ["deadbeef"]
    assert "baadf00d" not in adopted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_import.py::test_import_continues_after_one_group_fails -v`
Expected: FAIL - `OSError` propagates and the second (`ok_group`) never gets adopted.

- [ ] **Step 3: Implement**

In `src/omm/cli.py`, `_run_import_flow`'s loop:

```python
    bytes_saved = 0
    for group in groups:
        if group.sha256 not in selected_hashes:
            continue
        try:
            result = scan_import.adopt_group(group)
        except (OSError, linker.LinkError) as e:
            err_console.print(f"[yellow]Could not import {group.display_name}: {e}[/yellow]")
            continue
        bytes_saved += result.bytes_saved
        console.print(f"  [green]Imported {result.filename}[/green]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_import.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_import.py
git commit -m "$(cat <<'EOF'
fix: don't let one model's import failure abort the whole omm import run

A permission error moving one external .gguf into the hub - including
during the silent first-run auto-import - previously crashed the entire
command instead of skipping just that model and continuing.
EOF
)"
```

---

### Task 11: `omm benchmark --output` write failure

**Files:**
- Modify: `src/omm/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- No new names. `write_evidence` now raises `QualityEvaluationError` (already caught by `benchmark_cmd`'s existing `except quality_mod.QualityEvaluationError` block) instead of a raw `OSError`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_quality.py` (check its existing imports first and reuse `quality` module alias already in use there):

```python
def test_write_evidence_raises_quality_evaluation_error_on_write_failure(tmp_path, monkeypatch):
    from pathlib import Path

    bad_path = tmp_path / "does-not-exist-parent" / "evidence.json"
    monkeypatch.setattr(
        Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError("permission denied"))
    )

    try:
        quality.write_evidence({"models": []}, bad_path)
        assert False, "expected QualityEvaluationError"
    except quality.QualityEvaluationError:
        pass
```

(Use whatever the file's existing import alias for `omm.quality` is - check the top of `tests/test_quality.py` and match it; the examples above assume `from omm import quality`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_quality.py::test_write_evidence_raises_quality_evaluation_error_on_write_failure -v`
Expected: FAIL - raw `OSError` propagates instead of `QualityEvaluationError`.

- [ ] **Step 3: Implement**

In `src/omm/quality.py`, replace `write_evidence`:

```python
def write_evidence(report: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
    except OSError as e:
        raise QualityEvaluationError(f"Could not write evidence to {path}: {e}") from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_quality.py tests/test_cli_benchmark.py -v`
Expected: PASS - `benchmark_cmd`'s existing `except quality_mod.QualityEvaluationError as error: err_console.print(...); raise typer.Exit(1) from error` (cli.py ~2480-2482) already wraps the `write_evidence` call site, so no `cli.py` change is needed here - only the exception type it can now catch changes.

- [ ] **Step 5: Commit**

```bash
git add src/omm/quality.py tests/test_quality.py
git commit -m "$(cat <<'EOF'
fix: report a write failure on `omm benchmark --output` cleanly

write_evidence's OSError previously didn't match the QualityEvaluationError
except clause already wrapping its call site in benchmark_cmd, so a bad
--output path (unwritable parent, permission denied) crashed instead of
producing the command's normal red-error-and-exit-1 path.
EOF
)"
```

---

### Task 12: Remaining small `cli.py` file-op guards

**Files:**
- Modify: `src/omm/cli.py`
- Test: `tests/test_cli_remove.py`, `tests/test_cli_autoremove.py`, `tests/test_cli_link.py`, `tests/test_cli_upgrade.py`, `tests/test_cli_update.py`

**Interfaces:**
- No new names. `_cleanup_incomplete_install`, `_remove_one`, `_autoremove_incomplete_installs`, `omm link`'s directory creation, `_update_one`'s finalize step, and `_perform_update`'s exception handling all tolerate an `OSError` instead of crashing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_remove.py` (add `from pathlib import Path` to its imports):

```python
def test_uninstall_tolerates_permission_error_removing_model_file(isolated_omm_home, monkeypatch):
    filename = "a.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    real_unlink = Path.unlink

    def _flaky_unlink(self, missing_ok=False):
        if self == dest:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    result = runner.invoke(cli.app, ["uninstall", filename])

    assert result.exit_code == 0, result.stdout
    assert filename not in registry.load_registry()
```

Add to `tests/test_cli_autoremove.py` (add `from pathlib import Path`):

```python
def test_autoremove_tolerates_permission_error_removing_incomplete_file(isolated_omm_home, monkeypatch):
    _stub_no_new_engines(monkeypatch)
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    cli.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    orphan = cli.MODELS_DIR / "orphan.gguf"
    orphan.write_bytes(b"junk")

    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert orphan.exists()
```

Add to `tests/test_cli_link.py` (add `from pathlib import Path`):

```python
def test_link_reports_clean_error_when_directory_cannot_be_created(isolated_omm_home, tmp_path, monkeypatch):
    filename = "model.gguf"
    (cli.MODELS_DIR / filename).write_bytes(b"model")
    registry.save_registry({filename: {"linked": {}}})
    target = tmp_path / "custom-models"

    monkeypatch.setattr(
        Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError("permission denied"))
    )

    result = runner.invoke(cli.app, ["link", str(target)])

    assert result.exit_code == 1
    assert "Could not create" in result.stderr
```

Add to `tests/test_cli_upgrade.py`:

```python
def test_upgrade_direct_url_install_reports_skipped_when_finalize_fails(isolated_omm_home, monkeypatch):
    _no_engines(monkeypatch)
    dest = cli.MODELS_DIR / "model.gguf"
    dest.write_bytes(b"old-bytes")
    registry.save_registry({"model.gguf": _entry(repo_id=None, sha256="old-hash")})

    def fake_download(url, dest_path):
        Path(dest_path).write_bytes(b"brand-new-bytes")

    monkeypatch.setattr(cli, "download_file", fake_download)
    monkeypatch.setattr(
        Path, "replace", lambda self, target: (_ for _ in ()).throw(OSError("permission denied"))
    )

    result = runner.invoke(cli.app, ["upgrade", "model.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "failed to finalize" in result.stderr
    assert dest.read_bytes() == b"old-bytes"
    assert not (cli.MODELS_DIR / "model.gguf.update").exists()
```

Add to `tests/test_cli_update.py`:

```python
def test_update_reports_error_when_pipx_permission_denied(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda *a, **k: "new" * 13 + "new")
    monkeypatch.setattr(
        cli, "_git_update_src", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)

    def _raise(*args, **kwargs):
        raise PermissionError("pipx exists but is not executable")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "update failed" in result.stderr.lower()
    assert refresh_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli_remove.py tests/test_cli_autoremove.py tests/test_cli_link.py tests/test_cli_upgrade.py tests/test_cli_update.py -v -k "tolerates or cannot_be_created or finalize_fails or permission_denied"`
Expected: FAIL on every new test - each currently-uncaught `OSError` propagates.

- [ ] **Step 3: Implement**

In `src/omm/cli.py`:

1. `_cleanup_incomplete_install` (~line 1518):

```python
def _cleanup_incomplete_install(filename: str) -> bool:
    dest = MODELS_DIR / filename
    part = dest.with_suffix(dest.suffix + ".part")
    cleaned = False
    if part.exists():
        try:
            part.unlink()
            cleaned = True
        except OSError:
            pass
    if dest.exists():
        try:
            dest.unlink()
            cleaned = True
        except OSError:
            pass
    return cleaned
```

2. `_remove_one` (~line 1531), the `dest.unlink(missing_ok=True)` pair:

```python
    dest = MODELS_DIR / filename
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
    except OSError:
        pass

    registry.remove_entry(filename)
    console.print(f"[green]Removed {filename}[/green]")
```

3. `_autoremove_incomplete_installs` (~line 2403):

```python
def _autoremove_incomplete_installs() -> int:
    if not MODELS_DIR.exists():
        return 0

    reg = registry.load_registry()
    removed = 0
    for path in MODELS_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix == ".part":
            if path.with_suffix("").name not in reg:
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
        elif path.suffix == ".gguf" and path.name not in reg:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed
```

4. `omm link`'s directory creation (~line 2337-2339):

```python
    if directory is not None:
        directory = directory.expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            err_console.print(f"[red]Could not create {directory}: {error}[/red]")
            raise typer.Exit(1) from error
        linked_count = 0
        skipped_missing = 0
        ...
```

5. `_update_one`'s finalize step (~line 1776), inside the direct-URL branch:

```python
        new_sha256 = sha256_file(tmp)
        if new_sha256 == old_sha256:
            tmp.unlink(missing_ok=True)
            return "up_to_date"
        try:
            tmp.replace(dest)
        except OSError as e:
            err_console.print(f"[red]{filename}: update failed to finalize: {e}[/red]")
            tmp.unlink(missing_ok=True)
            return "skipped"
```

6. `_perform_update` (~line 778-798), broaden the exception coverage past `FileNotFoundError` alone:

```python
def _perform_update(branch: str) -> subprocess.CompletedProcess:
    """Shared by `omm update` and `omm setting version` (channel switch):
    migrate-or-pull SRC_DIR onto `branch`, reinstalling via pipx only if
    dependencies changed."""
    migrated = _src_head_commit() is not None
    try:
        if not migrated:
            return _migrate_to_editable_install(branch)
        console.print(f"Updating omm from {REPO_URL} ({branch}) ...")
        result = _git_update_src(branch)
        if result.returncode == 0 and not _deps_satisfied():
            result = _run_pipx_install_with_progress(
                ["pipx", "install", "--force", "--editable", _install_spec()]
            )
        return result
    except FileNotFoundError:
        err_console.print(
            "[red]git or pipx not found. Install them first, or rerun the installer:[/red]\n"
            "  curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh"
        )
        raise typer.Exit(1)
    except OSError as e:
        err_console.print(f"[red]Update failed: {e}[/red]")
        raise typer.Exit(1) from e
```

(`FileNotFoundError` is an `OSError` subclass, so keep it as the first, more specific `except` clause - Python tries clauses in order, so the specific "git or pipx not found" message still wins for that exact case; the new `except OSError` clause below it is the catch-all for everything else, e.g. a `PermissionError` from `Popen` or a failed `rename`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli_remove.py tests/test_cli_autoremove.py tests/test_cli_link.py tests/test_cli_upgrade.py tests/test_cli_update.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_remove.py tests/test_cli_autoremove.py tests/test_cli_link.py tests/test_cli_upgrade.py tests/test_cli_update.py
git commit -m "$(cat <<'EOF'
fix: tolerate permission errors in uninstall/autoremove/link/upgrade/update

Several small file operations (removing a model file, cleaning up an
orphaned partial download, creating a custom link directory, finalizing
an upgrade, reinstalling via pipx) had no OSError handling at all and
crashed with a raw traceback instead of a clean skip or error message.
EOF
)"
```

---

### Final Task: Full-suite verification

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: all tests pass, no warnings about unused imports left behind (e.g. `errno`/`Path`/`struct` added to test files but never used if a draft test above ends up trimmed).

- [ ] **Step 2: Manual smoke check (per `superpowers:verification-before-completion` - do not skip)**

In a scratch venv (see the "Isolated pipx/git testing recipe" pattern already used in this project - a `pipx install --suffix` + `HOME` override, never the developer's real `omm` install):

1. `omm install <a small real model>` with `shutil.disk_usage` unmodified (real check) - confirm normal installs are completely unaffected.
2. Simulate a full disk (e.g. a small `tmpfs`/loop-mounted filesystem, or temporarily point `MODELS_DIR` at a nearly-full volume) and run `omm install <model>` - confirm a clean one-line error, no traceback, `.part` file cleaned up.
3. Run `omm autoremove` against a deliberately-corrupted `link-ownership.json` permission scenario (e.g. `chmod 000` the file) - confirm a clean message via the `main()` safety net instead of a traceback, then restore permissions.

- [ ] **Step 3: Final commit (if the smoke check surfaces any fixups)**

Only if Step 2 finds something - otherwise no commit needed; the plan is done once Task 12's commit lands and the full suite is green.
