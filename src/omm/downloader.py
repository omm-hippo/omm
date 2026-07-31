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

import errno
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.progress import (
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.segment import Segment
from rich.style import StyleType
from rich.table import Column
from rich.text import Text

from omm.atomic import atomic_write_text

_CHUNK_SIZE = 1024 * 1024
_DEFAULT_THREADS = 4
_MIN_CHUNK_SIZE = 8 * 1024 * 1024  # minimum work per thread
_MIN_PARALLEL_TOTAL = 20 * 1024 * 1024  # below this, not worth parallelizing

_err_console = Console(stderr=True, highlight=False)

_RETRY_DELAYS = [1, 1, 3, 5, 10, 10, 10, 10, 10]
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1


class DownloadError(Exception):
    pass


class DownloadCancelled(DownloadError):
    pass


class InsufficientDiskSpaceError(DownloadError):
    pass


def _sidecar_path(part_path: Path) -> Path:
    """Path to the JSON file tracking per-range progress of a parallel
    download, so an interrupted download can resume only the unfinished
    byte ranges instead of restarting the whole file. `part_path` itself
    can't carry this info: `_download_parallel` pre-truncates it to the
    final size before any bytes arrive, so its file size alone can't tell
    a resume how much of each thread's range actually landed."""
    return part_path.with_name(part_path.name + ".ranges.json")


def _read_sidecar(sidecar_path: Path) -> dict | None:
    try:
        with sidecar_path.open("r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_sidecar(
    sidecar_path: Path,
    url: str,
    strong_etag: str,
    total_size: int,
    ranges: list[dict],
) -> None:
    # Write-to-temp-then-rename so a crash mid-write can't leave behind a
    # half-written JSON file that would poison the next resume attempt.
    tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(
            {
                "url": url,
                "etag": strong_etag,
                "total_size": total_size,
                "ranges": ranges,
            },
            f,
        )
    tmp.replace(sidecar_path)


@dataclass
class _HashBar:
    """Homebrew-style '#'-filled bar (plain ASCII, not rich's default Unicode
    half-block chars) so it matches `curl -#`. Clamped to
    [min_width, max_width] so it neither collapses on a narrow terminal nor
    stretches absurdly wide on an ultra-wide one."""

    completed: float
    total: float | None
    min_width: int = 10
    max_width: int = 60
    complete_style: StyleType = "bar.complete"
    back_style: StyleType = "bar.back"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(self.min_width, min(self.max_width, options.max_width))
        filled = min(width, round(width * self.completed / self.total)) if self.total else 0
        empty = width - filled
        if filled:
            yield Segment("#" * filled, console.get_style(self.complete_style))
        if empty:
            yield Segment(" " * empty, console.get_style(self.back_style))


class HashBarColumn(ProgressColumn):
    """Renders `_HashBar`, expanding to fill the space `Progress(expand=True)`
    hands its ratio-1 table column - same mechanism `BarColumn` itself uses
    for a full-width bar, just with '#' instead of Unicode blocks."""

    def __init__(self, min_width: int = 10, max_width: int = 60) -> None:
        self.min_width = min_width
        self.max_width = max_width
        super().__init__(table_column=Column(ratio=1))

    def render(self, task) -> _HashBar:
        return _HashBar(task.completed, task.total, self.min_width, self.max_width)


class EtaColumn(TimeRemainingColumn):
    """`TimeRemainingColumn(compact=True)` prefixed with 'ETA ' so it reads
    clearly next to size/speed instead of a bare, easy-to-miss timestamp."""

    def __init__(self) -> None:
        super().__init__(compact=True)

    def render(self, task) -> Text:
        inner = super().render(task)
        if not inner.plain:
            return inner
        return Text(f"ETA {inner.plain}", style=inner.style)


def _progress(*, quiet: bool = False, no_color: bool = False) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[blue]{task.fields[filename]}", table_column=Column(no_wrap=True)),
        HashBarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        EtaColumn(),
        expand=True,
        disable=quiet,
        console=Console(no_color=True) if no_color else None,
    )


def _strong_etag(headers) -> str | None:
    etag = (headers.get("ETag") or "").strip()
    return etag if etag and not etag.startswith("W/") else None


def _part_metadata_path(part_path: Path) -> Path:
    return part_path.with_name(f"{part_path.name}.meta")


def _load_resume_metadata(
    meta_path: Path, url: str, resume_pos: int
) -> tuple[str, int] | None:
    try:
        metadata = json.loads(meta_path.read_text())
        etag = metadata["etag"]
        total_size = metadata["total_size"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (
        metadata.get("url") != url
        or not isinstance(etag, str)
        or not etag
        or etag.startswith("W/")
        or not isinstance(total_size, int)
        or isinstance(total_size, bool)
        or total_size <= resume_pos
    ):
        return None
    return etag, total_size


def _write_resume_metadata(
    meta_path: Path, url: str, etag: str | None, total_size: int | None
) -> None:
    if etag is None or total_size is None or total_size <= 0:
        meta_path.unlink(missing_ok=True)
        return
    atomic_write_text(
        meta_path,
        json.dumps(
            {"url": url, "etag": etag, "total_size": total_size},
            sort_keys=True,
        ),
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


def _probe_range_support(url: str) -> tuple[int, bool, str | None]:
    """Probe with a 1-byte Range request.

    Returns total size, range support, and a strong ETag. Parallel download
    additionally requires the ETag so every range stays bound to one object.
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
        return 0, False, None
    strong_etag = _strong_etag(resp.headers)
    resp.close()
    content_range = (resp.headers.get("Content-Range") or "").strip()
    honored = resp.status_code == 206 or (
        resp.status_code == 200 and resp.headers.get("Content-Length") == "1"
    )
    match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
    if honored and match is not None:
        total = int(match.group(1))
        return total, total > 0, strong_etag
    return 0, False, None


def _download_range_worker(
    url: str,
    part_path: Path,
    sidecar_path: Path,
    range_state: dict,
    ranges_state: list[dict],
    total_size: int,
    strong_etag: str,
    progress: Progress,
    task_id,
    lock: threading.Lock,
    errors: list[Exception],
    stop_check: Callable[[], bool] | None,
) -> None:
    import requests

    start = range_state["start"]
    end = range_state["end"]
    resume_offset = start + range_state["done"]

    resp = None
    try:
        resp = requests.get(
            url,
            headers={
                "Range": f"bytes={resume_offset}-{end}",
                "If-Range": strong_etag,
            },
            stream=True,
            timeout=30,
        )
        expected_len = end - resume_offset + 1
        content_range = re.fullmatch(
            r"bytes (\d+)-(\d+)/(\d+|\*)",
            (resp.headers.get("Content-Range") or "").strip(),
        )
        range_matches = (
            content_range is not None
            and int(content_range.group(1)) == resume_offset
            and int(content_range.group(2)) == end
            and content_range.group(3) != "*"
            and int(content_range.group(3)) == total_size
        )
        response_etag = (resp.headers.get("ETag") or "").strip()
        honored = (resp.status_code == 206 and range_matches) or (
            resp.status_code == 200
            and resp.headers.get("Content-Length") == str(expected_len)
            and range_matches
        )
        if not honored or response_etag != strong_etag:
            raise DownloadError(
                f"Expected a Range response for bytes={resume_offset}-{end}, got "
                f"status {resp.status_code}, Content-Range "
                f"{resp.headers.get('Content-Range')!r}, and Content-Length "
                f"{resp.headers.get('Content-Length')!r}; response ETag "
                f"{response_etag!r} did not preserve {strong_etag!r}"
            )
        written = 0
        with part_path.open("r+b") as f:
            f.seek(resume_offset)
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                if written + len(chunk) > expected_len:
                    raise DownloadError(
                        f"Range bytes={resume_offset}-{end} returned more than "
                        f"{expected_len} bytes."
                    )
                try:
                    f.write(chunk)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise InsufficientDiskSpaceError(
                            f"Not enough disk space to download {part_path.name}."
                        ) from e
                    raise
                written += len(chunk)
                with lock:
                    range_state["done"] += len(chunk)
                    progress.update(task_id, advance=len(chunk))
                    _write_sidecar(
                        sidecar_path,
                        url,
                        strong_etag,
                        total_size,
                        ranges_state,
                    )
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")
        if written != expected_len:
            raise DownloadError(
                f"Range bytes={resume_offset}-{end} returned {written} bytes; "
                f"expected {expected_len}."
            )
    except Exception as e:  # noqa: BLE001 - collected and re-raised by the caller
        errors.append(e)
    finally:
        if resp is not None:
            resp.close()


def _run_range_workers(
    url: str,
    dest: Path,
    part_path: Path,
    sidecar_path: Path,
    total_size: int,
    ranges_state: list[dict],
    strong_etag: str,
    stop_check: Callable[[], bool] | None,
    *,
    quiet: bool = False,
    no_color: bool = False,
) -> None:
    """Download whichever ranges in `ranges_state` aren't yet `done`, in
    parallel, updating the sidecar after every chunk so a future resume
    only has to fetch what's still missing."""
    lock = threading.Lock()
    errors: list[Exception] = []
    completed = sum(r["done"] for r in ranges_state)
    pending = [r for r in ranges_state if r["done"] < (r["end"] - r["start"] + 1)]

    with _progress(quiet=quiet, no_color=no_color) as progress:
        task_id = progress.add_task(
            "download", total=total_size, completed=completed, filename=dest.name
        )
        if pending:
            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                futures = [
                    executor.submit(
                        _download_range_worker,
                        url,
                        part_path,
                        sidecar_path,
                        r,
                        ranges_state,
                        total_size,
                        strong_etag,
                        progress,
                        task_id,
                        lock,
                        errors,
                        stop_check,
                    )
                    for r in pending
                ]
                # A plain future.result() with no timeout blocks on a raw OS
                # wait. On Windows that can't be interrupted by Ctrl+C at all
                # (unlike POSIX, where a blocking syscall wakes on EINTR) until
                # the wait itself completes - so polling with a short timeout is
                # what lets a pending Ctrl+C actually get serviced.
                for future in futures:
                    while True:
                        try:
                            future.result(timeout=0.5)
                            break
                        except FutureTimeoutError:
                            continue

    if errors:
        cancelled = next((e for e in errors if isinstance(e, DownloadCancelled)), None)
        if cancelled is not None:
            raise cancelled
        disk_full = next((e for e in errors if isinstance(e, InsufficientDiskSpaceError)), None)
        if disk_full is not None:
            raise disk_full
        raise DownloadError(str(errors[0])) from errors[0]

    part_path.replace(dest)
    sidecar_path.unlink(missing_ok=True)


def _download_parallel(
    url: str,
    dest: Path,
    part_path: Path,
    sidecar_path: Path,
    total_size: int,
    thread_count: int,
    strong_etag: str,
    stop_check: Callable[[], bool] | None,
    *,
    quiet: bool = False,
    no_color: bool = False,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with part_path.open("wb") as f:
        f.truncate(total_size)

    ranges_state = [{"start": start, "end": end, "done": 0} for start, end in _plan_ranges(total_size, thread_count)]
    _write_sidecar(sidecar_path, url, strong_etag, total_size, ranges_state)

    _run_range_workers(
        url,
        dest,
        part_path,
        sidecar_path,
        total_size,
        ranges_state,
        strong_etag,
        stop_check,
        quiet=quiet, no_color=no_color,
    )


def _download_parallel_resume(
    url: str,
    dest: Path,
    part_path: Path,
    sidecar_path: Path,
    state: dict,
    stop_check: Callable[[], bool] | None,
    *,
    quiet: bool = False,
    no_color: bool = False,
) -> None:
    _run_range_workers(
        url,
        dest,
        part_path,
        sidecar_path,
        state["total_size"],
        state["ranges"],
        state["etag"],
        stop_check,
        quiet=quiet, no_color=no_color,
    )


def _download_single_stream(
    url: str,
    dest: Path,
    part_path: Path,
    stop_check: Callable[[], bool] | None,
    *,
    quiet: bool = False,
    no_color: bool = False,
    allow_clean_restart: bool = True,
) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = _part_metadata_path(part_path)
    resume_pos = part_path.stat().st_size if part_path.exists() else 0
    resume_etag: str | None = None
    resume_total: int | None = None
    if resume_pos:
        resume_metadata = _load_resume_metadata(meta_path, url, resume_pos)
        if resume_metadata is None:
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            resume_pos = 0
        else:
            resume_etag, resume_total = resume_metadata

    headers = (
        {
            "Range": f"bytes={resume_pos}-",
            "If-Range": resume_etag,
        }
        if resume_pos
        else {}
    )
    resp = requests.get(url, headers=headers, stream=True, timeout=30)

    if resume_pos and resp.status_code == 200:
        resume_pos = 0
        mode = "wb"
        raw_length = resp.headers.get("Content-Length")
        try:
            final_total = int(raw_length) if raw_length is not None else None
        except ValueError:
            final_total = None
        if final_total is not None and final_total < 0:
            final_total = None
        expected_response_bytes = final_total
    elif resp.status_code == 416:
        if not resume_pos:
            raise DownloadError(f"Download failed: HTTP 416 for {url}")
        resp.close()
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        return _download_single_stream(
            url,
            dest,
            part_path,
            stop_check,
            quiet=quiet,
            no_color=no_color,
            allow_clean_restart=False,
        )
    elif resp.status_code in (200, 206):
        resp.raise_for_status()
        if resp.status_code == 206:
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)",
                (resp.headers.get("Content-Range") or "").strip(),
            )
            range_start = int(match.group(1)) if match is not None else -1
            range_end = int(match.group(2)) if match is not None else -1
            range_total = int(match.group(3)) if match is not None else -1
            valid_resume = (
                resume_pos > 0
                and resume_etag is not None
                and resume_total is not None
                and range_start == resume_pos
                and range_end == range_total - 1
                and range_total == resume_total
                and _strong_etag(resp.headers) == resume_etag
            )
            if not valid_resume:
                resp.close()
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                if not allow_clean_restart:
                    raise DownloadError(
                        "Server returned an invalid Content-Range after a clean retry."
                    )
                return _download_single_stream(
                    url,
                    dest,
                    part_path,
                    stop_check,
                    quiet=quiet,
                    no_color=no_color,
                    allow_clean_restart=False,
                )
            expected_response_bytes = range_total - resume_pos
            final_total = range_total
            mode = "ab"
        else:
            raw_length = resp.headers.get("Content-Length")
            try:
                final_total = int(raw_length) if raw_length is not None else None
            except ValueError:
                final_total = None
            if final_total is not None and final_total < 0:
                final_total = None
            expected_response_bytes = final_total
            mode = "wb"
    else:
        raise DownloadError(f"Download failed: HTTP {resp.status_code} for {url}")

    if mode == "wb":
        with part_path.open("wb"):
            pass
    _write_resume_metadata(
        meta_path,
        url,
        resume_etag if mode == "ab" else _strong_etag(resp.headers),
        final_total,
    )
    written = 0

    with _progress(quiet=quiet, no_color=no_color) as progress:
        task = progress.add_task(
            "download",
            total=final_total or None,
            completed=resume_pos if mode == "ab" else 0,
            filename=dest.name,
        )
        with part_path.open("ab") as f:
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
                    written += len(chunk)
                    progress.update(task, advance=len(chunk))
                if stop_check is not None and stop_check():
                    raise DownloadCancelled("interrupted by user")

    resp.close()
    if expected_response_bytes is not None and written != expected_response_bytes:
        raise DownloadError(
            f"Download returned {written} bytes; expected {expected_response_bytes}."
        )
    if final_total is not None and part_path.stat().st_size != final_total:
        raise DownloadError(
            f"Downloaded file is {part_path.stat().st_size} bytes; "
            f"expected {final_total}."
        )
    part_path.replace(dest)
    meta_path.unlink(missing_ok=True)


def _attempt_download(
    url: str,
    dest: Path,
    part_path: Path,
    stop_check: Callable[[], bool] | None,
    *,
    quiet: bool = False,
    no_color: bool = False,
) -> None:
    sidecar_path = _sidecar_path(part_path)

    if part_path.exists() and sidecar_path.exists():
        state = _read_sidecar(sidecar_path)
        total_size, supports_ranges, strong_etag = _probe_range_support(url)
        if (
            isinstance(state, dict)
            and supports_ranges
            and strong_etag is not None
            and state.get("url") == url
            and state.get("etag") == strong_etag
            and state.get("total_size") == total_size
            and total_size == part_path.stat().st_size
        ):
            try:
                _download_parallel_resume(
                    url, dest, part_path, sidecar_path, state, stop_check,
                    quiet=quiet, no_color=no_color,
                )
                return
            except (DownloadCancelled, InsufficientDiskSpaceError):
                raise
            except DownloadError as e:
                if _is_retryable_network_error(e):
                    # Leave part_path + sidecar in place - the outer retry
                    # loop in download_file() will call back in here after
                    # its backoff, and this branch will resume the same
                    # ranges rather than restarting the whole file.
                    raise
                part_path.unlink(missing_ok=True)
                sidecar_path.unlink(missing_ok=True)
                # fall through to a clean single-stream retry
        else:
            # Stale or mismatched sidecar (e.g. left over from a different
            # URL/size) - can't trust its per-range progress, start clean.
            part_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)

    if not part_path.exists():
        total_size, supports_ranges, strong_etag = _probe_range_support(url)
        if (
            supports_ranges
            and strong_etag is not None
            and total_size >= _MIN_PARALLEL_TOTAL
        ):
            thread_count = _choose_thread_count(total_size)
            if thread_count > 1:
                try:
                    _download_parallel(
                        url,
                        dest,
                        part_path,
                        sidecar_path,
                        total_size,
                        thread_count,
                        strong_etag,
                        stop_check,
                        quiet=quiet, no_color=no_color,
                    )
                    return
                except (DownloadCancelled, InsufficientDiskSpaceError):
                    raise
                except DownloadError as e:
                    if _is_retryable_network_error(e):
                        raise
                    part_path.unlink(missing_ok=True)
                    sidecar_path.unlink(missing_ok=True)
                    # fall through to a clean single-stream retry

    _download_single_stream(url, dest, part_path, stop_check, quiet=quiet, no_color=no_color)


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


def download_file(
    url: str,
    dest: Path,
    stop_check: Callable[[], bool] | None = None,
    *,
    quiet: bool = False,
    no_color: bool = False,
) -> None:
    """Download `url` to `dest`.

    A fresh, range-capable download above `_MIN_PARALLEL_TOTAL` bytes is
    split across multiple threads for speed. Everything else - small files,
    servers that ignore Range, and resuming an existing validated partial
    file from a prior run goes through the single-stream path.

    Transient network errors (DNS failure, connection reset, timeout,
    truncated stream) are retried up to `_MAX_ATTEMPTS` times with a fixed
    fixed backoff schedule, reusing partial bytes only while their strong
    validator still matches. Non-network errors are not retried.

    If `stop_check` is given, it's polled regularly during the transfer and
    during backoff waits; a truthy result raises `DownloadCancelled` and
    leaves the partial in place for a later validated resume.

    `quiet` disables the progress bar (the retry warning below still prints -
    it's a warning, not decorative). `no_color` disables ANSI styling on both
    the progress bar and the retry warning."""
    part_path = dest.with_suffix(dest.suffix + ".part")
    if part_path.is_symlink():
        raise DownloadError(f"Refusing symlinked partial download path: {part_path}.")
    last_error: Exception | None = None
    if no_color:
        _err_console.no_color = True

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _attempt_download(url, dest, part_path, stop_check, quiet=quiet, no_color=no_color)
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
