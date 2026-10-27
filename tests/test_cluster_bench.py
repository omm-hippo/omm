"""`omm cluster benchmark`: time one streamed answer from the cluster."""

from __future__ import annotations

from pathlib import Path

from omm.cluster import bench, chat

FIX = Path(__file__).parent / "fixtures" / "exo"
MODEL = "mlx-community/Qwen3.5-9B-4bit"
POINT = chat.Endpoint("http://127.0.0.1:52415", None)


class _Response:
    def __init__(self, body: bytes):
        self._lines = iter(body.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def readline(self):
        return next(self._lines, b"")


def _clock(step=0.25):
    now = {"t": 0.0}

    def tick():
        now["t"] += step
        return now["t"]

    return tick


def test_measure_prefers_exo_generation_stats(monkeypatch):
    # A real exo 1.0.71 stream (Qwen3.5-9B-4bit, 2026-10-26) ends with a
    # ": generation_stats" comment carrying exo's own speeds.
    body = (FIX / "chat-stream-stats.txt").read_bytes()
    sent = {}

    def fake_open(point, method, path, payload, timeout):
        sent.update(payload)
        return _Response(body)

    monkeypatch.setattr(chat, "_open", fake_open)
    result = bench.measure(POINT, MODEL, clock=_clock())
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert result["source"] == "exo"
    assert result["output_tokens"] == 12
    assert result["decode_tokens_per_second"] > 0 and result["prompt_tokens_per_second"] > 0
    assert result["ttft_seconds"] > 0


def test_measure_falls_back_to_usage_and_its_own_clock(monkeypatch):
    body = (b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            b'data: {"choices":[],"usage":{"completion_tokens":10}}\n\n'
            b"data: [DONE]\n\n")
    monkeypatch.setattr(chat, "_open", lambda *a: _Response(body))
    result = bench.measure(POINT, MODEL, clock=_clock(0.5))
    assert result["source"] == "usage" and result["output_tokens"] == 10
    assert result["prompt_tokens_per_second"] is None
    assert result["decode_tokens_per_second"] > 0


def test_measure_counts_pieces_without_usage(monkeypatch):
    body = (b'data: {"choices":[{"delta":{"reasoning_content":"x"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"y"}}]}\n\n'
            b"data: [DONE]\n\n")
    monkeypatch.setattr(chat, "_open", lambda *a: _Response(body))
    result = bench.measure(POINT, MODEL, clock=_clock())
    assert result["source"] == "pieces" and result["output_tokens"] == 2
