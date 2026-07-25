# Download Network Retry/Backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retry transient network failures during `omm install` downloads with a fixed backoff schedule instead of letting a raw `requests` exception blow up into a full traceback.

**Architecture:** `downloader.py`'s existing probe/parallel/single-stream dispatch logic moves unchanged into a new `_attempt_download()` helper. `download_file()` becomes a thin retry loop around it: up to 10 attempts, waiting `1, 1, 3, 5, 10, 10, 10, 10, 10` seconds between them, printing one status line per retry, and re-raising a clean `DownloadError` after the last attempt. Each retry reuses the same `.part` file so the existing Range-resume logic picks the transfer back up — no new resume mechanism.

**Tech Stack:** Python, `requests`, `rich.console.Console` for status output, `pytest` + `monkeypatch` for tests (existing patterns in `tests/test_downloader.py`).

## Global Constraints

- Retry schedule is fixed at exactly `1, 1, 3, 5, 10, 10, 10, 10, 10` seconds (10 total attempts). Do not make it configurable — YAGNI.
- Only retry `requests.exceptions.ConnectionError`, `requests.exceptions.Timeout`, `requests.exceptions.ChunkedEncodingError` (directly, or as `__cause__` of a `DownloadError` from the parallel path). Everything else (HTTP status errors, `DownloadCancelled`) fails immediately as today.
- No change to `cli.py` — it already handles `DownloadError` cleanly.
- No change to `_probe_range_support()` — already degrades gracefully.
- Tests must not actually sleep for the real durations — monkeypatch `downloader.time.sleep`.

---

### Task 1: Extract `_attempt_download()` and add the retry loop

**Files:**
- Modify: `src/omm/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Produces: `downloader._RETRY_DELAYS: list[int]` (the 9 wait values above), `downloader._MAX_ATTEMPTS: int` (10), `downloader._is_retryable_network_error(exc: BaseException) -> bool`, `downloader._sleep_with_stop_check(seconds: float, stop_check: Callable[[], bool] | None) -> None`, `downloader._attempt_download(url: str, dest: Path, part_path: Path, stop_check: Callable[[], bool] | None) -> None` (the old body of `download_file`, unchanged behavior). `download_file()`'s public signature is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_downloader.py`:

```python
# --- network retry/backoff ---------------------------------------------------


def test_download_file_retries_transient_network_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    dest = tmp_path / "model.gguf"
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("boom")
        return _FakeResp(200, [b"hello", b"world"])

    monkeypatch.setattr(requests, "get", flaky_get)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == b"helloworld"
    assert calls["n"] == 3


def test_download_file_raises_download_error_after_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    sleeps = []
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: sleeps.append(seconds))
    dest = tmp_path / "model.gguf"
    calls = {"n": 0}

    def always_fails(*a, **k):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", always_fails)

    raised = None
    try:
        downloader.download_file("https://example.com/model.gguf", dest)
    except downloader.DownloadError as e:
        raised = e

    assert raised is not None
    assert calls["n"] == downloader._MAX_ATTEMPTS
    assert sleeps == downloader._RETRY_DELAYS


def test_download_file_does_not_retry_on_permanent_http_status_error(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    dest = tmp_path / "model.gguf"
    calls = {"n": 0}

    def not_found(*a, **k):
        calls["n"] += 1
        return _FakeResp(404, [])

    monkeypatch.setattr(requests, "get", not_found)

    raised = None
    try:
        downloader.download_file("https://example.com/model.gguf", dest)
    except downloader.DownloadError as e:
        raised = e

    assert raised is not None
    assert calls["n"] == 1  # no retry


def test_download_file_cancellation_during_backoff_wait_raises_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    dest = tmp_path / "model.gguf"

    def always_fails(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", always_fails)

    raised = None
    try:
        downloader.download_file(
            "https://example.com/model.gguf", dest, stop_check=lambda: True
        )
    except downloader.DownloadCancelled as e:
        raised = e

    assert raised is not None
```

Note: `_FakeResp(404, [])` needs `raise_for_status()` to actually raise for this test to reach the existing "no retry" HTTP-status path — check `_download_single_stream`'s handling of non-200/206/416 codes (`src/omm/downloader.py:195-196`, currently `raise DownloadError(...)` directly for other status codes, not via `raise_for_status()`), so status 404 already raises `DownloadError` directly without going through `requests.exceptions.HTTPError`. No change needed to `_FakeResp` — it already fits this path since 404 falls into the `else: raise DownloadError(...)` branch.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_downloader.py -k "retry or backoff or max_attempts or cancellation_during" -v`
Expected: FAIL (AttributeError on `downloader.time` or `downloader._MAX_ATTEMPTS` not existing, or the retry not happening).

- [ ] **Step 3: Implement the retry loop**

In `src/omm/downloader.py`:

Add `import time` to the top-level imports (alongside `import threading`), and add `from rich.console import Console` next to the existing `from rich.progress import (...)` import.

Add a module-level console right after the existing constants (after `_MIN_PARALLEL_TOTAL = ...`):

```python
_err_console = Console(stderr=True)

_RETRY_DELAYS = [1, 1, 3, 5, 10, 10, 10, 10, 10]
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1
```

Rename the existing body of `download_file()` into a new `_attempt_download()` function, placed just above `download_file()`:

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
                except DownloadCancelled:
                    raise
                except DownloadError:
                    part_path.unlink(missing_ok=True)
                    # fall through to a clean single-stream retry

    _download_single_stream(url, dest, part_path, stop_check)


def _is_retryable_network_error(exc: BaseException) -> bool:
    import requests

    network_errors = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )
    if isinstance(exc, network_errors):
        return True
    return isinstance(exc, DownloadError) and isinstance(exc.__cause__, network_errors)


def _sleep_with_stop_check(seconds: float, stop_check: Callable[[], bool] | None) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if stop_check is not None and stop_check():
            raise DownloadCancelled("interrupted by user")
        time.sleep(min(0.5, remaining))
```

Replace `download_file()`'s body with the retry loop (keep its existing docstring, extend it to mention retry):

```python
def download_file(url: str, dest: Path, stop_check: Callable[[], bool] | None = None) -> None:
    """Download `url` to `dest`.

    A fresh, range-capable download above `_MIN_PARALLEL_TOTAL` bytes is
    split across multiple threads for speed. Everything else - small files,
    servers that ignore Range, and resuming an existing `.part` file from a
    prior run (single- or multi-threaded feature version alike) - goes
    through the single-stream path, which also handles resuming.

    Transient network errors (DNS failure, connection reset, timeout,
    truncated stream) are retried up to `_MAX_ATTEMPTS` times with a fixed
    backoff schedule (`_RETRY_DELAYS`), reusing the same `.part` file across
    attempts. Non-network errors (bad HTTP status, etc.) are not retried.

    If `stop_check` is given, it's polled regularly during the transfer and
    during backoff waits; a truthy result raises `DownloadCancelled` and
    leaves the `.part` file in place for a later resume (used by `omm
    contribute`'s Esc-to-stop)."""
    part_path = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _attempt_download(url, dest, part_path, stop_check)
            return
        except DownloadCancelled:
            raise
        except Exception as e:
            if not _is_retryable_network_error(e):
                raise
            last_error = e
            if attempt == _MAX_ATTEMPTS:
                break
            delay = _RETRY_DELAYS[attempt - 1]
            _err_console.print(
                f"[yellow]네트워크 오류, {delay}초 후 재시도 ({attempt + 1}/{_MAX_ATTEMPTS})...[/yellow]"
            )
            _sleep_with_stop_check(delay, stop_check)

    raise DownloadError(
        f"네트워크 연결 실패: {_MAX_ATTEMPTS}번 시도 후 포기 ({last_error})"
    ) from last_error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: all PASS, including the 4 new tests and every pre-existing test in the file (they must be unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/omm/downloader.py tests/test_downloader.py
git commit -m "feat: retry transient network errors during download with backoff"
```

## Self-Review Notes

- Spec coverage: schedule (✓ `_RETRY_DELAYS`), retryable-exception scope (✓ `_is_retryable_network_error`), resume reuse (✓ same `part_path` passed every attempt), status output (✓ one `_err_console.print` per retry, no traceback), cancellation during wait (✓ `_sleep_with_stop_check`), exhaustion → clean `DownloadError` (✓), non-retryable HTTP status unaffected (✓ test 3), no `cli.py` changes (✓), no `_probe_range_support` changes (✓).
- Single task: this is a one-file, cohesive change: extraction + retry loop are inseparable pieces of the same edit, so splitting into multiple tasks would be artificial.
