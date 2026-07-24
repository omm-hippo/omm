import threading

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


def test_download_file_completes_normally_without_stop_check(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(
        downloader.requests, "get", lambda *a, **k: _FakeResp(200, [b"hello", b"world"])
    )

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == b"helloworld"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_file_raises_cancelled_and_keeps_part_file_when_stop_check_fires(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(
        downloader.requests, "get", lambda *a, **k: _FakeResp(200, [b"hello", b"world", b"!!!"])
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
        downloader.requests,
        "get",
        lambda *a, **k: _FakeResp(206, [b"x"], headers={"Content-Range": "bytes 0-0/5000000"}),
    )

    total, capable = downloader._probe_range_support("https://example.com/m.gguf")

    assert total == 5000000
    assert capable is True


def test_probe_range_support_not_capable_on_200(monkeypatch):
    monkeypatch.setattr(
        downloader.requests,
        "get",
        lambda *a, **k: _FakeResp(200, [b"x"], headers={"Content-Length": "5000000"}),
    )

    total, capable = downloader._probe_range_support("https://example.com/m.gguf")

    assert capable is False


def test_probe_range_support_handles_network_error(monkeypatch):
    def _raise(*a, **k):
        raise downloader.requests.RequestException("boom")

    monkeypatch.setattr(downloader.requests, "get", _raise)

    total, capable = downloader._probe_range_support("https://example.com/m.gguf")

    assert total == 0
    assert capable is False


def test_probe_range_support_accepts_200_with_matching_content_length(monkeypatch):
    """ModelScope's download endpoint honors Range but replies 200, not 206
    - confirmed live (see docs/superpowers/specs/2026-07-24-multi-provider-hub-design.md).
    A single byte requested and exactly one byte returned, with a Content-Range
    header proving the server sliced correctly, must count as Range support."""

    class _FakeResp:
        status_code = 200
        headers = {"Content-Range": "bytes 0-0/491400032", "Content-Length": "1"}

        def close(self):
            pass

    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges = downloader._probe_range_support("https://example.com/f.gguf")
    assert total == 491400032
    assert supports_ranges is True


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

    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: _FakeResp())
    total, supports_ranges = downloader._probe_range_support("https://example.com/f.gguf")
    assert supports_ranges is False


# --- end-to-end dispatcher behavior -----------------------------------------


class _FakeRangeServer:
    """Fake `requests.get` for a range-capable server: bytes=0-0 probes the
    total size; bytes=<start>-<end> returns exactly that slice as a 206."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests: list[str] = []

    def __call__(self, url, headers=None, stream=True, timeout=30):
        range_header = (headers or {}).get("Range", "")
        self.requests.append(range_header)
        if range_header == "bytes=0-0":
            return _FakeResp(
                206, [self.payload[:1]], headers={"Content-Range": f"bytes 0-0/{len(self.payload)}"}
            )
        start_str, end_str = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_str), int(end_str)
        chunk = self.payload[start : end + 1]
        return _FakeResp(206, [chunk])


def test_download_file_uses_parallel_path_and_produces_correct_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 10)
    monkeypatch.setattr(downloader, "_MIN_CHUNK_SIZE", 5)
    payload = bytes(range(40)) * 1  # 40 distinct-ish bytes
    server = _FakeRangeServer(payload)
    monkeypatch.setattr(downloader.requests, "get", server)
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

    def fake_get(url, headers=None, stream=True, timeout=30):
        # server ignores Range entirely, always returns the full body as 200
        return _FakeResp(200, [payload], headers={"Content-Length": str(len(payload))})

    monkeypatch.setattr(downloader.requests, "get", fake_get)
    dest = tmp_path / "model.gguf"

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == payload


def test_download_file_resumes_existing_part_file_via_single_stream(tmp_path, monkeypatch):
    dest = tmp_path / "model.gguf"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"hello")
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=30):
        calls.append((headers or {}).get("Range"))
        return _FakeResp(206, [b"world"])

    monkeypatch.setattr(downloader.requests, "get", fake_get)

    downloader.download_file("https://example.com/model.gguf", dest)

    assert dest.read_bytes() == b"helloworld"
    assert calls == ["bytes=5-"]  # single resume request, no probe


def test_download_file_parallel_path_honors_stop_check(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_choose_thread_count", lambda total: 1)
    monkeypatch.setattr(downloader, "_MIN_PARALLEL_TOTAL", 1)
    payload = b"x" * 30

    def fake_get(url, headers=None, stream=True, timeout=30):
        range_header = (headers or {}).get("Range", "")
        if range_header == "bytes=0-0":
            return _FakeResp(206, [payload[:1]], headers={"Content-Range": f"bytes 0-0/{len(payload)}"})
        return _FakeResp(206, [payload[i : i + 5] for i in range(0, len(payload), 5)])

    monkeypatch.setattr(downloader.requests, "get", fake_get)
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
