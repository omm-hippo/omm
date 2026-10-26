"""`omm cluster run`: chat with the model the cluster serves."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest
from typer.testing import CliRunner

from omm import cli, rag_remote
from omm.cluster import chat, gateway, settings, tokens
from omm.cluster.commands import UsageProblem
from omm.cluster.errors import ClusterError

FIXTURES = Path(__file__).parent / "fixtures" / "exo"
LOADED = "mlx-community/Llama-3.2-1B-Instruct-4bit"
# Shape of a real exo 1.0.71 stream through the gateway (Qwen3.5-9B-4bit, 2026-10-26).
EXO_STREAM = (
    b": keep-alive\n\n"
    b': prefill_progress {"PrefillProgressChunk":{"processed_tokens":0,"total_tokens":13}}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null,"reasoning_content":"Think"}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":null,"reasoning_content":"ing."}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":"\\n\\n","reasoning_content":null}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":" Hello","reasoning_content":null}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":" there","reasoning_content":null}}]}\n\n'
    b"data: [DONE]\n\n"
)


class _FakeExo(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeExoHandler)
        self.state = json.loads((FIXTURES / "state-1node.json").read_text(encoding="utf-8"))
        self.chat_bodies: list[dict] = []


class _FakeExoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/state":
            self._send(200, json.dumps(self.server.state).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.chat_bodies.append(json.loads(self.rfile.read(length)))
        self._send(200, EXO_STREAM, "text/event-stream")


def _start(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def exo():
    server = _start(_FakeExo())
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def head(exo, isolated_omm_home):
    token = tokens.add_token("kim")
    server = _start(gateway.GatewayServer("127.0.0.1", 0, upstream=f"http://127.0.0.1:{exo.server_address[1]}"))
    yield chat.Endpoint(f"http://127.0.0.1:{server.server_address[1]}", token)
    server.shutdown()
    server.server_close()


def test_gateway_lists_only_loaded_model_ids(head):
    assert chat.loaded_models(head) == [LOADED]


def test_gateway_loaded_models_needs_a_token(head):
    with pytest.raises(ClusterError, match="rejected your token"):
        chat.loaded_models(chat.Endpoint(head.url, "omm_wrong"))


def test_gateway_loaded_models_is_502_when_exo_is_down(isolated_omm_home):
    import socket

    token = tokens.add_token("kim")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    server = _start(gateway.GatewayServer("127.0.0.1", 0, upstream=f"http://127.0.0.1:{dead}"))
    try:
        with pytest.raises(ClusterError, match="exo is not running"):
            chat.loaded_models(chat.Endpoint(f"http://127.0.0.1:{server.server_address[1]}", token))
    finally:
        server.shutdown()
        server.server_close()


def test_reply_streams_reasoning_and_answer_through_the_gateway(head, exo):
    pieces = []
    answer = chat.reply(head, LOADED, [{"role": "user", "content": "hi"}], lambda k, t: pieces.append((k, t)))
    assert answer == "Hello there"
    assert pieces == [("reasoning", "Think"), ("reasoning", "ing."), ("content", "Hello"), ("content", " there")]
    assert exo.chat_bodies == [{"model": LOADED, "messages": [{"role": "user", "content": "hi"}], "stream": True}]


def test_parse_stream_raises_on_an_error_event():
    lines = [b'data: {"error": {"message": "out of memory"}}\n']
    with pytest.raises(ClusterError, match="out of memory"):
        list(chat.parse_stream(iter(lines)))


def test_pick_model():
    assert chat.pick_model(None, ["a"]) == "a"
    assert chat.pick_model("b", ["a", "b"]) == "b"
    with pytest.raises(ClusterError, match="omm cluster model"):
        chat.pick_model(None, [])
    with pytest.raises(UsageProblem, match="omm cluster run b"):
        chat.pick_model(None, ["a", "b"])
    with pytest.raises(ClusterError, match="not loaded"):
        chat.pick_model("c", ["a"])


def test_endpoint_on_a_cluster_mac_is_local_exo(isolated_omm_home):
    settings.save(settings.ClusterSettings("member", "omm-0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d", 52440))
    assert chat.endpoint() == chat.Endpoint(chat.LOCAL_EXO, None)


def test_endpoint_uses_the_saved_head(isolated_omm_home):
    rag_remote.save(rag_remote.Remote("http://10.0.0.4:52440", "omm_tok"))
    assert chat.endpoint() == chat.Endpoint("http://10.0.0.4:52440", "omm_tok")


def test_endpoint_without_a_head_points_to_rag_remote_set(isolated_omm_home):
    with pytest.raises(UsageProblem, match="omm rag remote set"):
        chat.endpoint()


runner = CliRunner()


def test_cli_run_chats_until_bye_and_keeps_history(monkeypatch, isolated_omm_home):
    point = chat.Endpoint("http://head:52440", "omm_tok")
    seen = []

    def fake_reply(endpoint, model, messages, on_piece):
        seen.append([dict(m) for m in messages])
        on_piece("reasoning", "hmm")
        on_piece("content", f"answer {len(seen)}")
        return f"answer {len(seen)}"

    monkeypatch.setattr(chat, "endpoint", lambda: point)
    monkeypatch.setattr(chat, "loaded_models", lambda p: [LOADED])
    monkeypatch.setattr(chat, "reply", fake_reply)
    result = runner.invoke(cli.app, ["cluster", "run"], input="first\n\nsecond\n/bye\n")
    assert result.exit_code == 0, result.output
    assert f"{LOADED} on the cluster head at http://head:52440" in " ".join(result.output.split())
    assert "hmm" in result.output and "answer 2" in result.output
    assert seen[1] == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer 1"},
                       {"role": "user", "content": "second"}]


def test_cli_run_without_a_head_explains_the_setup(isolated_omm_home):
    result = runner.invoke(cli.app, ["cluster", "run"])
    assert result.exit_code == 2
    assert "omm rag remote set <head address>" in result.output


def test_cli_run_keeps_chatting_after_a_failed_turn(monkeypatch, isolated_omm_home):
    calls = []

    def flaky_reply(endpoint, model, messages, on_piece):
        calls.append([dict(m) for m in messages])
        if len(calls) == 1:
            raise ClusterError("the model stopped with an error: boom")
        return "ok"

    monkeypatch.setattr(chat, "endpoint", lambda: chat.Endpoint(chat.LOCAL_EXO, None))
    monkeypatch.setattr(chat, "loaded_models", lambda p: [LOADED])
    monkeypatch.setattr(chat, "reply", flaky_reply)
    result = runner.invoke(cli.app, ["cluster", "run"], input="one\ntwo\n/bye\n")
    assert result.exit_code == 0
    assert "boom" in result.output
    assert calls[1] == [{"role": "user", "content": "two"}]
