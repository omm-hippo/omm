import errno
import io
import json
from pathlib import Path
from types import SimpleNamespace

import requests
from rich.console import Console
import pytest

from omm import downloader


class _FakeResp:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {}

    def iter_content(self, chunk_size):
        yield from self._chunks

    def raise_for_status(self):
        pass

    def close(self):
        pass


def test_replace_with_retry_succeeds_after_transient_permission_error(tmp_path, monkeypatch):
    """POSIX rename() silently succeeds over an open file; Windows raises
    PermissionError instead (e.g. AV briefly holding the destination).
    Most such locks clear within a second or two."""
    part = tmp_path / "model.gguf.part"
    dest = tmp_path / "model.gguf"
    part.write_bytes(b"data")
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    calls = {"n": 0}
    real_replace = Path.replace

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("WinError 32")
        return real_replace(self, target)

    monkeypatch.setattr(downloader.Path, "replace", flaky_replace)

    downloader._replace_with_retry(part, dest)

    assert dest.read_bytes() == b"data"
    assert calls["n"] == 3


def test_replace_with_retry_raises_download_error_after_exhausting_attempts(tmp_path, monkeypatch):
    part = tmp_path / "model.gguf.part"
    dest = tmp_path / "model.gguf"
    part.write_bytes(b"data")
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)

    def always_locked(self, target):
        raise PermissionError("WinError 32")

    monkeypatch.setattr(downloader.Path, "replace", always_locked)

    with pytest.raises(downloader.DownloadError, match="Could not finalize"):
        downloader._replace_with_retry(part, dest, attempts=3)


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
    assert len(get_calls) == 2  # probe + single-stream, not retried


def test_download_file_completes_normally_without_stop_check(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResp(200, [b"hello", b"world"])
    )

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == b"helloworld"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_file_refuses_symlinked_partial_path(
    tmp_path, monkeypatch, requires_symlink_support
):
    dest = tmp_path / "model.gguf"
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    dest.with_suffix(dest.suffix + ".part").symlink_to(victim)
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")),
    )

    with pytest.raises(downloader.DownloadError, match="symlinked partial"):
        downloader.download_file("https://example.com/model.gguf", dest)

    assert victim.read_bytes() == b"preserve"


@pytest.mark.parametrize(
    ("requested", "final"),
    [
        ("http://example.com/model.gguf", "http://example.com/model.gguf"),
        ("https://example.com/model.gguf", "http://cdn.example.com/model.gguf"),
    ],
)
def test_download_file_rejects_http_and_redirect_downgrade(
    tmp_path, monkeypatch, requested, final
):
    response = _FakeResp(200, [b"untrusted"])
    response.url = final
    monkeypatch.setattr(requests, "get", lambda *a, **k: response)

    with pytest.raises(downloader.DownloadError, match="non-HTTPS|HTTPS-to-HTTP"):
        downloader.download_file(requested, tmp_path / "model.gguf")


def test_https_redirect_downgrade_is_rejected_before_following(tmp_path, monkeypatch):
    calls = []

    def redirect_once(url, **kwargs):
        calls.append(url)
        response = _FakeResp(302, [], headers={"Location": "http://cdn.example/model.gguf"})
        response.url = url
        return response

    monkeypatch.setattr(requests, "get", redirect_once)

    with pytest.raises(downloader.DownloadError, match="HTTPS-to-HTTP"):
        downloader.download_file("https://example.com/model.gguf", tmp_path / "model.gguf")

    assert calls == ["https://example.com/model.gguf"]


def test_download_file_raises_cancelled_and_keeps_part_file_when_stop_check_fires(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResp(200, [b"hello", b"world", b"!!!"])
    )
    calls = []

    def stop_check():
        calls.append(1)
        return len(calls) >= 2  # stop after the second chunk is written

    raised = False
    try:
        downloader.download_file("https://example.com/model.gguf", dest, stop_check=stop_check)
    except downloader.DownloadCancelled:
        raised = True

    assert raised
    assert not dest.exists()
    part = dest.with_suffix(dest.suffix + ".part")
    assert part.exists()
    assert part.read_bytes() == b"helloworld"


# --- pure logic: range planning / thread count -----------------------------


def test_plan_ranges_sums_to_total_with_no_gaps_or_overlap():
    ranges = downloader._plan_ranges(1000, 4)

    assert ranges[0][0] == 0
    assert ranges[-1][1] == 999
    total_covered = 0
    for i, (start, end) in enumerate(ranges):
        assert end >= start
        total_covered += end - start + 1
        if i > 0:
            assert start == ranges[i - 1][1] + 1
    assert total_covered == 1000
    assert len(ranges) == 4


def test_plan_ranges_single_thread_covers_whole_file():
    assert downloader._plan_ranges(500, 1) == [(0, 499)]


@pytest.mark.parametrize(
    "ranges",
    [
        [{"start": 0, "end": 9, "done": -1}],
        [{"start": 0, "end": 9, "done": 11}],
        [
            {"start": 0, "end": 5, "done": 6},
            {"start": 5, "end": 9, "done": 5},
        ],
        [{"start": 1, "end": 9, "done": 0}],
        "not-a-list",
    ],
)
def test_parallel_resume_state_rejects_invalid_range_maps(ranges):
    state = {"total_size": 10, "ranges": ranges}

    assert downloader._valid_parallel_state(state, 10) is False


def test_parallel_resume_state_accepts_exact_contiguous_coverage():
    state = {
        "total_size": 10,
        "ranges": [
            {"start": 0, "end": 4, "done": 5},
            {"start": 5, "end": 9, "done": 2},
        ],
    }

    assert downloader._valid_parallel_state(state, 10) is True


def test_choose_thread_count_below_min_parallel_total_returns_one():
    assert downloader._choose_thread_count(1024) == 1


def test_choose_thread_count_caps_at_default_max():
    huge = downloader._MIN_CHUNK_SIZE * 100
    assert downloader._choose_thread_count(huge) == downloader._DEFAULT_THREADS


def test_choose_thread_count_scales_with_size_between_bounds():
    size = downloader._MIN_PARALLEL_TOTAL + downloader._MIN_CHUNK_SIZE  # ~2 chunks worth
    n = downloader._choose_thread_count(size)
    assert 1 <= n <= downloader._DEFAULT_THREADS


# --- range-support probing ---------------------------------------------------


def test_probe_range_support_parses_content_range_on_206(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResp(
            206,
            [b"x"],
            headers={"Content-Range": "bytes 0-0/5000000", "ETag": '"v1"'},
        ),
    )

    total, capable, etag = downloader._probe_range_support("https://example.com/m.gguf")

    assert total == 5000000
    assert capable is True
    assert etag == '"v1"'


def test_probe_range_support_not_capable_on_200(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResp(200, [b"x"], headers={"Content-Length": "5000000"}),
    )

    total, capable, etag = downloader._probe_range_support("https://example.com/m.gguf")

    assert capable is False
    assert etag is None


def test_probe_range_support_handles_network_error(monkeypatch):
    def _raise(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", _raise)

    total, capable, etag = downloader._probe_range_support("https://example.com/m.gguf")

    assert total == 0
    assert capable is False
    assert etag is None


def test_probe_range_support_accepts_200_with_matching_content_length(monkeypatch):
    """ModelScope's download endpoint honors Range but replies 200, not 206
    - confirmed live (see docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md).
    A single byte requested and exactly one byte returned, with a Content-Range
    header proving the server sliced correctly, must count as Range support."""

    class _FakeResp:
        status_code = 200
        headers = {
            "Content-Range": "bytes 0-0/491400032",
            "Content-Length": "1",
            "ETag": '"v1"',
        }

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges, etag = downloader._probe_range_support(
        "https://example.com/f.gguf"
    )
    assert total == 491400032
    assert supports_ranges is True
    assert etag == '"v1"'


def test_probe_range_support_rejects_200_with_mismatched_content_length(monkeypatch):
    """A 200 response with a Content-Range header present (clearing the
    first guard) but whose Content-Length doesn't match the requested
    1-byte probe must still be rejected - this isolates the Content-Length
    equality check itself, not just the "no Content-Range at all" case
    already covered by test_probe_range_support_not_capable_on_200."""

    class _FakeResp:
        status_code = 200
        headers = {
            "Content-Range": "bytes 0-491400031/491400032",
            "Content-Length": "491400032",  # full file, not the 1 byte requested
        }

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges, etag = downloader._probe_range_support(
        "https://example.com/f.gguf"
    )
    assert supports_ranges is False
    assert etag is None


# --- end-to-end dispatcher behavior -----------------------------------------


class _FakeRangeServer:
    """Fake `requests.get` for a range-capable server: bytes=0-0 probes the
    total size; bytes=<start>-<end> returns exactly that slice as a 206."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests: list[str] = []

    def __call__(self, url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        self.requests.append(range_header)
        if range_header == "bytes=0-0":
            return _FakeResp(
                206,
                [self.payload[:1]],
                headers={
                    "Content-Range": f"bytes 0-0/{len(self.payload)}",
                    "ETag": '"v1"',
                },
            )
        start_str, end_str = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_str), int(end_str)
        chunk = self.payload[start : end + 1]
        return _FakeResp(
            206,
            [chunk],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
                "ETag": '"v1"',
            },
        )


def test_probe_range_support_rejects_wrong_probe_range(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResp(
            206,
            [b"x"],
            headers={"Content-Range": "bytes 1-1/5000000"},
        ),
    )

    assert downloader._probe_range_support("https://example.com/m.gguf") == (
        0,
        False,
        None,
    )


def test_parallel_download_rejects_wrong_worker_range_and_restarts_cleanly(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    payload = bytes(range(40))
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        calls.append(range_header)
        if range_header == "bytes=0-0":
            return _FakeResp(
                206,
                [payload[:1]],
                headers={
                    "Content-Range": f"bytes 0-0/{len(payload)}",
                    "ETag": '"v1"',
                },
            )
        if range_header:
            requested_start, requested_end = (
                int(value)
                for value in range_header.removeprefix("bytes=").split("-")
            )
            chunk = payload[requested_start : requested_end + 1]
            return _FakeResp(
                206,
                [chunk],
                headers={
                    "Content-Range": (
                        f"bytes 0-{len(chunk) - 1}/{len(payload)}"
                    ),
                    "ETag": '"v1"',
                },
            )
        return _FakeResp(
            200,
            [payload],
            headers={"Content-Length": str(len(payload))},
        )

    monkeypatch.setattr(requests, "get", fake_get)
    dest = tmp_path / "model.gguf"

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert calls[-1] == ""


def test_parallel_download_requires_one_strong_etag_for_every_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    payload = bytes(range(40))
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        calls.append((range_header, (headers or {}).get("If-Range")))
        if range_header == "bytes=0-0":
            return _FakeResp(
                206,
                [payload[:1]],
                headers={
                    "Content-Range": f"bytes 0-0/{len(payload)}",
                    "ETag": '"v1"',
                },
            )
        if range_header:
            start, end = (
                int(value)
                for value in range_header.removeprefix("bytes=").split("-")
            )
            return _FakeResp(
                206,
                [payload[start : end + 1]],
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(payload) + 1}",
                    "ETag": '"v2"',
                },
            )
        return _FakeResp(
            200,
            [payload],
            headers={"Content-Length": str(len(payload))},
        )

    monkeypatch.setattr(requests, "get", fake_get)
    dest = tmp_path / "model.gguf"

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert all(if_range == '"v1"' for _, if_range in calls[1:-1])
    assert calls[-1] == ("", None)


def test_download_file_uses_parallel_path_and_produces_correct_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    payload = bytes(range(40)) * 1  # 40 distinct-ish bytes
    server = _FakeRangeServer(payload)
    monkeypatch.setattr(requests, "get", server)
    dest = tmp_path / "model.gguf"

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    # first request is the probe; remaining requests are distinct byte ranges
    assert server.requests[0] == "bytes=0-0"
    assert len(server.requests) > 2  # probe + multiple range workers


def test_download_file_falls_back_to_single_stream_when_range_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 1)
    payload = b"a small file that ignores range requests"

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        # server ignores Range entirely, always returns the full body as 200
        return _FakeResp(200, [payload], headers={"Content-Length": str(len(payload))})

    monkeypatch.setattr(requests, "get", fake_get)
    dest = tmp_path / "model.gguf"

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload


def test_download_file_resumes_existing_part_file_via_single_stream(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"hello")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"v1"', 10
    )
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        calls.append(
            (
                (headers or {}).get("Range"),
                (headers or {}).get("If-Range"),
            )
        )
        return _FakeResp(
            206,
            [b"world"],
            headers={"Content-Range": "bytes 5-9/10", "ETag": '"v1"'},
        )

    monkeypatch.setattr(requests, "get", fake_get)

    downloader.download_file(url, dest)

    assert dest.read_bytes() == b"helloworld"
    assert calls == [("bytes=5-", '"v1"')]  # single bound resume request, no probe
    assert not downloader._part_metadata_path(part).exists()


def test_download_file_discards_legacy_partial_without_validator(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"old-prefix")
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        calls.append(headers or {})
        return _FakeResp(200, [b"fresh"], headers={"Content-Length": "5"})

    monkeypatch.setattr(requests, "get", fake_get)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == b"fresh"
    assert calls == [{}]


def test_changed_resume_truncates_old_prefix_before_recording_new_etag(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"old-prefix")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"old"', 20
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResp(
            200,
            [b"new-body"],
            headers={"Content-Length": "8", "ETag": '"new"'},
        ),
    )

    def crash_while_recording(*args, **kwargs):
        assert part.read_bytes() == b""
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(downloader, "atomic_write_text", crash_while_recording)

    with pytest.raises(RuntimeError, match="simulated crash"):
        downloader.download_file(url, dest)

    assert part.read_bytes() == b""


def test_download_file_restarts_cleanly_after_416(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"truncated")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"v1"', 20
    )
    responses = iter(
        [
            _FakeResp(416, []),
            _FakeResp(200, [b"complete"], headers={"Content-Length": "8"}),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: next(responses))

    downloader.download_file(url, dest)

    assert dest.read_bytes() == b"complete"
    assert not part.exists()


def test_download_file_does_not_trust_same_length_partial_after_416(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"complete")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"v1"', 10
    )
    responses = iter(
        [
            _FakeResp(
                416, [], headers={"Content-Range": f"bytes */{part.stat().st_size}"}
            ),
            _FakeResp(200, [b"verified"], headers={"Content-Length": "8"}),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: next(responses))

    downloader.download_file(url, dest)

    assert dest.read_bytes() == b"verified"
    assert not part.exists()


def test_download_file_restarts_when_206_range_does_not_match_request(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"stale")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"v1"', 10
    )
    responses = iter(
        [
            _FakeResp(
                206,
                [b"wrong"],
                headers={"Content-Range": "bytes 0-4/10", "ETag": '"v1"'},
            ),
            _FakeResp(200, [b"fresh"], headers={"Content-Length": "5"}),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: next(responses))

    downloader.download_file(url, dest)

    assert dest.read_bytes() == b"fresh"


def test_download_file_retries_short_resumed_body_from_last_written_byte(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"hello")
    url = "https://example.com/model.gguf"
    downloader._write_resume_metadata(
        downloader._part_metadata_path(part), url, '"v1"', 10
    )
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    requested_ranges = []

    def short_once_then_finish(url, headers=None, **kwargs):
        requested_range = (headers or {}).get("Range")
        requested_ranges.append(requested_range)
        if requested_range == "bytes=5-":
            return _FakeResp(
                206,
                [b"wor"],
                headers={"Content-Range": "bytes 5-9/10", "ETag": '"v1"'},
            )
        assert requested_range == "bytes=8-"
        return _FakeResp(
            206,
            [b"ld"],
            headers={"Content-Range": "bytes 8-9/10", "ETag": '"v1"'},
        )

    monkeypatch.setattr(requests, "get", short_once_then_finish)

    downloader.download_file(url, dest)

    assert dest.read_bytes() == b"helloworld"
    assert requested_ranges == ["bytes=5-", "bytes=8-"]


def test_download_file_rejects_partial_206_for_clean_request(
    tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    dest.with_suffix(dest.suffix + ".part").write_bytes(b"")
    responses = iter(
        [
            _FakeResp(
                206, [b"tail"], headers={"Content-Range": "bytes 5-8/9"}
            ),
            _FakeResp(
                206, [b"tail"], headers={"Content-Range": "bytes 5-8/9"}
            ),
        ]
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: next(responses))

    with pytest.raises(downloader.DownloadError, match="invalid Content-Range"):
        downloader.download_file("https://example.com/model.gguf", dest)

    assert not dest.exists()


# --- network retry/backoff ---------------------------------------------------


def test_download_file_retries_transient_network_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    dest = tmp_path / "model.gguf"
    dest.with_suffix(dest.suffix + ".part").write_bytes(b"")  # skip range probing
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
    dest.with_suffix(dest.suffix + ".part").write_bytes(b"")  # skip range probing
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
    assert sum(sleeps) == sum(downloader._RETRY_DELAYS)


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
    assert calls["n"] == 2  # probe + single-stream attempt, no retry


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


def test_download_file_parallel_path_honors_stop_check(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 1)
    payload = b"x" * 30

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        if range_header == "bytes=0-0":
            return _FakeResp(206, [payload[:1]], headers={"Content-Range": f"bytes 0-0/{len(payload)}"})
        return _FakeResp(200, [payload[i : i + 5] for i in range(0, len(payload), 5)])

    monkeypatch.setattr(requests, "get", fake_get)
    dest = tmp_path / "model.gguf"
    calls = []

    def stop_check():
        calls.append(1)
        return len(calls) >= 2

    raised = False
    try:
        downloader.download_file("https://example.com/model.gguf", dest, stop_check=stop_check)
    except downloader.DownloadCancelled:
        raised = True

    assert raised
    assert not dest.exists()
    assert dest.with_suffix(dest.suffix + ".part").exists()


# --- parallel-download resume via sidecar ------------------------------------


class _DropsAfterFirstByte:
    """A 206 response whose `iter_content` yields exactly one byte and then
    raises `ChunkedEncodingError`, the way `requests` surfaces a connection
    reset mid-stream (as opposed to a clean end of the response body)."""

    status_code = 206
    def __init__(self, first_byte: bytes, headers: dict):
        self._first_byte = first_byte
        self.headers = headers

    def iter_content(self, chunk_size):
        yield self._first_byte
        raise requests.exceptions.ChunkedEncodingError("connection reset")

    def raise_for_status(self):
        pass

    def close(self):
        pass


def test_download_file_resumes_parallel_download_after_network_drop_without_restarting(
    tmp_path, monkeypatch
):
    """The bug this guards: a 4-thread parallel download interrupted by a
    transient network error (laptop sleep, wifi drop) used to unlink the
    whole `.part` file and restart from byte 0. It should now resume only
    the bytes the affected range still needs, not the whole file."""
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)
    payload = bytes(range(40))  # 40 bytes -> 4 ranges of 10 bytes each: 0-9,10-19,20-29,30-39
    dest = tmp_path / "model.gguf"

    range_headers_seen: list[str] = []
    dropped_once = {"done": False}
    etag = '"model-v1"'

    def flaky_server(url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        range_headers_seen.append(range_header)
        if range_header == "bytes=0-0":
            return _FakeResp(
                206,
                [payload[:1]],
                headers={
                    "Content-Range": f"bytes 0-0/{len(payload)}",
                    "ETag": etag,
                },
            )
        # The first request for range 0-9 drops after 1 byte, exactly once.
        # Whichever thread happens to service it (thread scheduling order
        # isn't deterministic), only its very first attempt is dropped.
        if range_header == "bytes=0-9" and not dropped_once["done"]:
            dropped_once["done"] = True
            return _DropsAfterFirstByte(
                payload[0:1],
                {
                    "Content-Range": f"bytes 0-9/{len(payload)}",
                    "ETag": etag,
                },
            )
        start_str, end_str = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_str), int(end_str)
        return _FakeResp(
            206,
            [payload[start : end + 1]],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                "ETag": etag,
            },
        )

    monkeypatch.setattr(requests, "get", flaky_server)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert not dest.with_suffix(dest.suffix + ".part.ranges.json").exists()
    # The retry for range 0-9 must ask for byte 1 onward, not byte 0 again -
    # proof it resumed the range instead of restarting the whole file.
    assert "bytes=1-9" in range_headers_seen
    assert dropped_once["done"]


def test_download_parallel_resume_only_refetches_unfinished_ranges(tmp_path, monkeypatch):
    """Directly exercise `_attempt_download`'s sidecar-resume branch: a
    `.part` + matching sidecar on disk (as left behind by an interrupted
    parallel download) must resume with per-range `Range` headers computed
    from each range's recorded progress, not redownload finished ranges or
    restart unfinished ones from their start."""
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    payload = bytes(range(20))  # two 10-byte ranges: 0-9 (done) and 10-19 (4/10 done)
    part.write_bytes(payload[:10] + payload[10:14] + b"\x00" * 6)

    sidecar = downloader._sidecar_path(part)
    etag = '"model-v1"'
    state = {
        "url": "https://example.com/model.gguf",
        "etag": etag,
        "total_size": 20,
        "ranges": [
            {"start": 0, "end": 9, "done": 10},  # already complete
            {"start": 10, "end": 19, "done": 4},  # 4 bytes already landed
        ],
    }
    sidecar.write_text(json.dumps(state))

    requested_ranges = []
    monkeypatch.setattr(
        downloader,
        "_probe_range_support",
        lambda url: (len(payload), True, etag),
    )

    def fake_get(url, headers=None, stream=True, timeout=30, **kwargs):
        range_header = (headers or {}).get("Range", "")
        requested_ranges.append(range_header)
        # Only the unfinished range (bytes 14-19) should ever be requested.
        assert range_header == "bytes=14-19"
        return _FakeResp(
            206,
            [payload[14:20]],
            headers={"Content-Range": "bytes 14-19/20", "ETag": etag},
        )

    monkeypatch.setattr(requests, "get", fake_get)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert requested_ranges == ["bytes=14-19"]  # never re-requested the completed range
    assert not sidecar.exists()


def test_download_parallel_falls_back_cleanly_on_non_network_download_error(tmp_path, monkeypatch):
    """A structural (non-network) DownloadError from the parallel path -
    e.g. a server that stops honoring Range mid-download - must still drop
    the `.part`/sidecar and fall back to a clean single-stream download,
    same as before this change."""
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    payload = bytes(range(40))
    dest = tmp_path / "model.gguf"
    calls = {"n": 0}

    def bad_range_server(url, headers=None, stream=True, timeout=30, **kwargs):
        calls["n"] += 1
        range_header = (headers or {}).get("Range", "")
        if range_header == "bytes=0-0":
            return _FakeResp(
                206, [payload[:1]], headers={"Content-Range": f"bytes 0-0/{len(payload)}"}
            )
        if calls["n"] == 2:
            # Server stops honoring Range for this worker: full 200 body.
            return _FakeResp(200, [payload], headers={"Content-Length": str(len(payload))})
        if not range_header:
            # The single-stream fallback's fresh (non-resuming) request.
            return _FakeResp(200, [payload], headers={"Content-Length": str(len(payload))})
        start_str, end_str = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_str), int(end_str)
        return _FakeResp(206, [payload[start : end + 1]])

    monkeypatch.setattr(requests, "get", bad_range_server)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert not dest.with_suffix(dest.suffix + ".part.ranges.json").exists()


def test_download_file_parallel_path_converts_enospc_write_error_to_insufficient_disk_space_error(tmp_path, monkeypatch):
    """Verify ENOSPC in a range worker is converted to InsufficientDiskSpaceError,
    propagates out without retry, and doesn't retry single-stream fallback."""
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    dest = tmp_path / "model.gguf"

    # Setup a range-capable server that will be called for probe + range requests
    payload = bytes(range(40)) * 1  # 40 bytes, enough to split into multiple ranges
    server = _FakeRangeServer(payload)
    monkeypatch.setattr(requests, "get", server)

    # Track writes across all file objects; fail on second write to trigger ENOSPC
    # during a range worker's write (not the truncate).
    write_count = [0]

    class _PartiallyFullDiskFile:
        def __init__(self, path, mode):
            self.path = path
            self.mode = mode
            self.file = None

        def __enter__(self):
            # Use real file for underlying storage; we'll intercept writes.
            self.file = open(self.path, self.mode)
            return self

        def __exit__(self, *exc_info):
            if self.file:
                self.file.close()
            return False

        def truncate(self, size=None):
            # Allow truncate (used by _download_parallel to pre-allocate)
            return self.file.truncate(size)

        def seek(self, pos):
            return self.file.seek(pos)

        def write(self, data):
            write_count[0] += 1
            # Fail on second write: first write succeeds (from first range worker),
            # second write fails (from another range worker or attempt).
            if write_count[0] >= 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self.file.write(data)

    original_open = Path.open
    def fake_open(self, mode="r", *args, **kwargs):
        # Intercept part_path.open to use our wrapper; let other opens pass
        # through with whatever arguments they were given - the text-mode
        # sidecar write passes encoding="utf-8", and swallowing it here would
        # make this double silently disagree with the code under test.
        if "b" in mode and self.name.endswith(".part"):
            return _PartiallyFullDiskFile(self, mode)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    raised = None
    try:
        downloader.download_file("https://example.com/model.gguf", dest)
    except downloader.InsufficientDiskSpaceError as e:
        raised = e

    # Verify the error is InsufficientDiskSpaceError (not generic DownloadError).
    assert raised is not None
    # Verify no retry happened: probe (1) + range workers (at least 1) = at least 2 GET calls.
    # If there were a retry (which there shouldn't be), we'd see another probe + range workers.
    # So we expect exactly the requests from the first attempt.
    assert server.requests[0] == "bytes=0-0"  # First request is the probe.
    assert len(server.requests) >= 2  # Probe + at least one range worker before ENOSPC.
    # Verify part_path still exists (not cleaned up yet, pending next retry).
    assert dest.with_suffix(dest.suffix + ".part").exists()


def _render_bar(completed, total, console_width, min_width=10, max_width=60):
    column = downloader.HashBarColumn(min_width=min_width, max_width=max_width)
    bar = column.render(SimpleNamespace(completed=completed, total=total))
    console = Console(file=io.StringIO(), width=console_width, color_system=None)
    options = console.options.update(max_width=console_width)
    segments = list(bar.__rich_console__(console, options))
    return "".join(segment.text for segment in segments)


def test_hash_bar_fills_proportionally_to_completed_ratio():
    text = _render_bar(completed=50, total=100, console_width=40)
    assert text == "#" * 20 + " " * 20


def test_hash_bar_uses_only_hash_and_space_no_brackets_or_percent():
    text = _render_bar(completed=30, total=100, console_width=40)
    assert set(text) <= {"#", " "}


def test_hash_bar_clamps_to_max_width_on_wide_terminal():
    text = _render_bar(completed=50, total=100, console_width=200)
    assert len(text) == 60
    assert text == "#" * 30 + " " * 30


def test_hash_bar_clamps_to_min_width_on_narrow_terminal():
    text = _render_bar(completed=50, total=100, console_width=5)
    assert len(text) == 10
    assert text == "#" * 5 + " " * 5


def test_hash_bar_fully_filled_at_completion():
    text = _render_bar(completed=100, total=100, console_width=40)
    assert text == "#" * 40


def test_hash_bar_renders_empty_when_total_unknown():
    text = _render_bar(completed=0, total=None, console_width=40)
    assert text == " " * 40


def _fake_task(**overrides):
    fields = dict(finished=False, finished_time=None, time_remaining=None, total=100)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_eta_column_shows_eta_prefixed_compact_minutes_seconds():
    task = _fake_task(time_remaining=95)  # 1:35
    assert downloader.EtaColumn().render(task).plain == "ETA 01:35"


def test_eta_column_shows_placeholder_when_time_remaining_unknown():
    task = _fake_task(time_remaining=None, total=100)
    assert downloader.EtaColumn().render(task).plain == "ETA --:--"


def test_eta_column_shows_nothing_when_total_unknown():
    task = _fake_task(time_remaining=None, total=None)
    assert downloader.EtaColumn().render(task).plain == ""


def test_progress_factory_renders_single_line_without_percent_or_legacy_bar_chars():
    progress = downloader._progress()
    task_id = progress.add_task(
        "download", total=5_600_000_000, completed=700_000_000, filename="ornith-1.0-9b-Q4_K_M.gguf"
    )
    console = Console(file=io.StringIO(), width=120, color_system=None)
    console.print(progress.make_tasks_table(progress.tasks))
    output = console.file.getvalue()

    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert "ornith-1.0-9b-Q4_K_M.gguf" in line
    assert "#" in line
    assert "%" not in line
    assert "[" not in line and "]" not in line
    assert not any(ch in line for ch in "━╸╺")
    assert task_id is not None


def test_progress_factory_disables_bar_when_quiet():
    assert downloader._progress(quiet=True).disable is True
    assert downloader._progress().disable is False


def test_progress_factory_forces_no_color_console_when_requested():
    assert downloader._progress(no_color=True).console.no_color is True


def test_download_file_quiet_disables_progress_bar(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(200, [b"hello"]))
    seen = {}
    real_progress = downloader._progress

    def spy_progress(*, quiet=False, no_color=False):
        seen["quiet"] = quiet
        seen["no_color"] = no_color
        return real_progress(quiet=quiet, no_color=no_color)

    monkeypatch.setattr(downloader, "_progress", spy_progress)

    downloader.download_file("https://example.com/model.gguf", dest, quiet=True, no_color=True)

    assert seen == {"quiet": True, "no_color": True}


def test_download_file_retry_warning_still_prints_when_quiet(tmp_path, monkeypatch):
    """quiet suppresses the progress bar, not the network-retry warning -
    that's a warning, and warnings always print (see #80)."""
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_probe_range_support", lambda url: (0, False, None))
    monkeypatch.setattr(downloader, "_sleep_with_stop_check", lambda seconds, stop_check: None)
    attempts = [
        requests.exceptions.ConnectionError("boom"),
        _FakeResp(200, [b"hello"]),
    ]

    def fake_get(*a, **k):
        result = attempts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(requests, "get", fake_get)
    printed = []
    monkeypatch.setattr(downloader._err_console, "print", lambda msg: printed.append(msg))

    downloader.download_file("https://example.com/model.gguf", dest, quiet=True)

    assert len(printed) == 1
    assert "재시도" in printed[0]


def test_download_file_refuses_a_concurrent_writer(
    isolated_omm_home, tmp_path, monkeypatch
):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(
        downloader,
        "_attempt_download",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not touch a shared partial download")
        ),
    )

    with downloader.locked(downloader._download_lock_path(dest)):
        with pytest.raises(downloader.DownloadError, match="already writing"):
            downloader.download_file("https://example.com/model.gguf", dest)
