"""Resumable file downloads with a Rich progress bar.

Fresh, range-capable downloads above `_MIN_PARALLEL_TOTAL` are split across
multiple concurrent HTTP connections (`_download_parallel`) to cut wall-clock
time on fast links where a single TCP stream doesn't saturate bandwidth.
Everything else - small files, servers that ignore Range, and resuming an
existing `.part` file from a prior run - goes through the original
single-stream path (`_download_single_stream`), which is also what makes a
resume after an interrupted parallel download simple and safe: it just
finishes the file over one connection rather than re-planning ranges.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

_CHUNK_SIZE = 1024 * 1024
_DEFAULT_THREADS = 4
_MIN_CHUNK_SIZE = 8 * 1024 * 1024  # minimum work per thread
_MIN_PARALLEL_TOTAL = 20 * 1024 * 1024  # below this, not worth parallelizing

_err_console = Console(stderr=True)

_RETRY_DELAYS = [1, 1, 3, 5, 10, 10, 10, 10, 10]
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1


class DownloadError(Exception):
    pass


class DownloadCancelled(DownloadError):
    pass


def _progress() -> Progress:
    return Progress(
        TextColumn("[cyan]{task.fields[filename]}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )


def _choose_thread_count(total_size: int, max_threads: int = _DEFAULT_THREADS) -> int:
    if total_size < _MIN_PARALLEL_TOTAL:
        return 1
    return max(1, min(max_threads, total_size // _MIN_CHUNK_SIZE))


def _plan_ranges(total_size: int, num_threads: int) -> list[tuple[int, int]]:
    """Split `[0, total_size)` into `num_threads` contiguous, non-overlapping
    inclusive byte ranges."""
    if num_threads <= 1:
        return [(0, total_size - 1)]
    base = total_size // num_threads
    ranges = []
    start = 0
    for i in range(num_threads):
        end = total_size - 1 if i == num_threads - 1 else start + base - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _probe_range_support(url: str) -> tuple[int, bool]:
    """Probe with a 1-byte Range request. Returns (total_size, supports_ranges).
    A 206 with a parseable `Content-Range` means the server (and by
    extension its CDN) honors Range requests, so a full download can be
    safely split across threads. Some servers (confirmed: ModelScope's
    download endpoint) honor the Range header - returning exactly the
    requested byte(s) with a correct Content-Range - but reply with status
    200 instead of the RFC-correct 206; a 200 only counts as Range support
    when Content-Length matches the single byte we asked for, so a server
    that ignores Range and dumps the whole file with status 200 isn't
    mistaken for one that sliced it."""
    import requests

    try:
        resp = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=30)
    except requests.RequestException:
        return 0, False
    resp.close()
    content_range = resp.headers.get("Content-Range", "")
    honored = resp.status_code == 206 or (
        resp.status_code == 200 and resp.headers.get("Content-Length") == "1"
    )
    if honored and content_range:
        try:
            total = int(content_range.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            return 0, False
        return total, total > 0
    return 0, False


def _download_range_worker(
    url: str,
    part_path: Path,
    start: int,
    end: int,
    progress: Progress,
    task_id,
    lock: threading.Lock,
    errors: list[Exception],
    stop_check: Callable[[], bool] | None,
) -> None:
    import requests

    try:
        resp = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=30)
        expected_len = end - start + 1
        honored = resp.status_code == 206 or (
            resp.status_code == 200
            and resp.headers.get("Content-Length") == str(expected_len)
        )
        if not honored:
            raise DownloadError(
                f"Expected a Range response for bytes={start}-{end}, got "
                f"status {resp.status_code} with Content-Length "
                f"{resp.headers.get('Content-Length')}"
            )
        with part_path.open("r+b") as f:
            f.seek(start)
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                with lock:
                    progress.update(task_id, advance=len(chunk))
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")
    except Exception as e:  # noqa: BLE001 - collected and re-raised by the caller
        errors.append(e)


def _download_parallel(
    url: str,
    dest: Path,
    part_path: Path,
    total_size: int,
    thread_count: int,
    stop_check: Callable[[], bool] | None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with part_path.open("wb") as f:
        f.truncate(total_size)

    ranges = _plan_ranges(total_size, thread_count)
    lock = threading.Lock()
    errors: list[Exception] = []

    with _progress() as progress:
        task_id = progress.add_task("download", total=total_size, completed=0, filename=dest.name)
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [
                executor.submit(
                    _download_range_worker,
                    url,
                    part_path,
                    start,
                    end,
                    progress,
                    task_id,
                    lock,
                    errors,
                    stop_check,
                )
                for start, end in ranges
            ]
            for future in futures:
                future.result()

    if errors:
        cancelled = next((e for e in errors if isinstance(e, DownloadCancelled)), None)
        if cancelled is not None:
            raise cancelled
        raise DownloadError(str(errors[0])) from errors[0]

    part_path.rename(dest)


def _download_single_stream(
    url: str, dest: Path, part_path: Path, stop_check: Callable[[], bool] | None
) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    resume_pos = part_path.stat().st_size if part_path.exists() else 0

    headers = {"Range": f"bytes={resume_pos}-"} if resume_pos else {}
    resp = requests.get(url, headers=headers, stream=True, timeout=30)

    if resume_pos and resp.status_code == 200:
        # Server ignored the Range request; restart from scratch.
        resume_pos = 0
        mode = "wb"
    elif resp.status_code == 416:
        # Already fully downloaded.
        part_path.rename(dest)
        return
    elif resp.status_code in (200, 206):
        resp.raise_for_status()
        mode = "ab" if resume_pos and resp.status_code == 206 else "wb"
    else:
        raise DownloadError(f"Download failed: HTTP {resp.status_code} for {url}")

    total = int(resp.headers.get("Content-Length", 0)) + resume_pos

    with _progress() as progress:
        task = progress.add_task("download", total=total or None, completed=resume_pos, filename=dest.name)
        with part_path.open(mode) as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    progress.update(task, advance=len(chunk))
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")

    part_path.rename(dest)


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
    remaining = seconds
    tick = 0.5
    while remaining > 0:
        if stop_check is not None and stop_check():
            raise DownloadCancelled("interrupted by user")
        wait = min(tick, remaining)
        time.sleep(wait)
        remaining -= wait


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
