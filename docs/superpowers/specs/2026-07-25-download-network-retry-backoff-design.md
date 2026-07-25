# Download network retry with backoff

## Problem

Transient network drops (Wi-Fi hiccup, DNS blip) during `omm install` hit
`requests.get()` inside `downloader.py`'s `_download_single_stream()`
uncaught. The raw exception (`requests.exceptions.ConnectionError`, etc.)
propagates all the way up through Typer, which prints a full multi-panel
Rich traceback to the terminal instead of a clean message.

`_download_parallel()`'s workers already catch broad `Exception` and
convert it to `DownloadError`, but that conversion happens immediately —
no retry, no backoff.

## Design

Add a retry loop around the download attempt in `download_file()`
(`src/omm/downloader.py`):

- **Schedule**: 10 attempts total, waits between them of
  `1, 1, 3, 5, 10, 10, 10, 10, 10` seconds.
- **Retryable exceptions**: `requests.exceptions.ConnectionError`,
  `requests.exceptions.Timeout`, `requests.exceptions.ChunkedEncodingError`.
  For `DownloadError` raised out of `_download_parallel()`, retry only if
  `__cause__` is one of the above.
- **Non-retryable**: `DownloadCancelled` (propagates immediately), and any
  `DownloadError` whose cause isn't a network-transient exception (e.g. bad
  HTTP status) — those still fail immediately as today.
- **Resume reuse**: each retry re-enters the same attempt path against the
  same `.part` file, so `_download_single_stream`'s existing resume-by-Range
  logic picks up where the last attempt left off. No new resume logic needed.
- **Status output**: one line per retry via a stderr `Console`, e.g.
  `네트워크 오류, 5초 후 재시도 (4/10)...`. No traceback, no per-chunk noise.
- **Cancellation during backoff wait**: sleep in short ticks, polling
  `stop_check` so `omm contribute`'s Esc-to-stop still works during a wait.
- **Exhaustion**: after the 10th failed attempt, raise `DownloadError` with a
  short final message. `cli.py`'s existing
  `except DownloadError as e: err_console.print(...); raise typer.Exit(1)`
  handles it — no `cli.py` changes needed.

## Out of scope

- Retrying on HTTP status errors (404/403/etc.) — those are deterministic,
  not network flakiness.
- Jitter / exponential backoff beyond the fixed schedule above.
- Changing `_probe_range_support()` — it already degrades gracefully
  (returns `(0, False)` on request failure) and isn't the source of the
  traceback.

## Testing

- Unit test the retry loop in isolation: mock `requests.get` to raise
  `ConnectionError` N times then succeed, assert it retries and succeeds
  without raising.
- Unit test exhaustion: mock to always raise, assert `DownloadError` after
  exactly 10 attempts, with no unhandled exception type leaking out.
- Unit test non-retryable path is unaffected (e.g. `DownloadCancelled` still
  propagates immediately, HTTP-status `DownloadError` still fails immediately).
