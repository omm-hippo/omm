"""Token-checked streaming gateway in front of exo (#503)."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading

import pytest

from omm.cluster import gateway, tokens

SSE_BYTES = b"event: a\ndata: 1\n\n" + b"event: b\ndata: 2\n\n" + b"event: c\ndata: 3\n\n"


class _Upstream(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.calls = 0
        self.last_headers = {}
        self.last_body = b""
        self.last_path = ""
        self.SSE_BYTES = SSE_BYTES


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _serve(self):
        server = self.server
        server.calls += 1
        server.last_headers = dict(self.headers.items())
        server.last_path = self.path
        length = int(self.headers.get("Content-Length") or 0)
        server.last_body = self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in SSE_BYTES.split(b"\n\n")[:-1]:
            chunk = piece + b"\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    do_GET = do_POST = do_DELETE = _serve


def _start(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server


@pytest.fixture
def upstream():
    server = _start(_Upstream())
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def token(isolated_omm_home):
    return tokens.add_token("kim")


@pytest.fixture
def gateway_server(upstream, token):
    server = _start(gateway.GatewayServer("127.0.0.1", 0, upstream=f"http://127.0.0.1:{upstream.server_address[1]}"))
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def gateway_without_upstream(token):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    server = _start(gateway.GatewayServer("127.0.0.1", 0, upstream=f"http://127.0.0.1:{dead}"))
    yield server
    server.shutdown()
    server.server_close()


def request(server, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    return conn.getresponse()


def post(server, path, headers=None, body=b"{}"):
    return request(server, "POST", path, headers, body)


def test_constants():
    assert gateway.GATEWAY_PORT == 52440
    assert gateway.UPSTREAM == "http://127.0.0.1:52415"
    assert gateway.MAX_BODY_BYTES == 16 * 1024 * 1024
    assert gateway.ALLOWED_ROUTES == frozenset({
        ("POST", "/v1/messages"), ("POST", "/v1/chat/completions"), ("POST", "/v1/responses"), ("GET", "/v1/models")})


def test_valid_bearer_is_proxied_and_streamed(gateway_server, upstream, token):
    resp = post(gateway_server, "/v1/messages", headers={"Authorization": f"Bearer {token}"}, body=b'{"x":1}')
    assert resp.status == 200 and resp.read() == SSE_BYTES
    assert "authorization" not in {k.lower() for k in upstream.last_headers}
    assert upstream.last_body == b'{"x":1}'


def test_query_string_is_ignored_for_matching_and_forwarded(gateway_server, upstream, token):
    resp = post(gateway_server, "/v1/messages?beta=true", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200
    resp.read()
    assert upstream.last_path == "/v1/messages?beta=true"


def test_x_api_key_is_accepted_and_stripped(gateway_server, upstream, token):
    resp = post(gateway_server, "/v1/messages", headers={"x-api-key": token})
    assert resp.status == 200
    resp.read()
    assert "x-api-key" not in {k.lower() for k in upstream.last_headers}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer omm_wrong"}, {"Authorization": "Basic abc"}])
def test_missing_or_bad_token_is_401_and_never_reaches_upstream(gateway_server, upstream, headers):
    resp = post(gateway_server, "/v1/messages", headers=headers)
    assert resp.status == 401
    body = json.loads(resp.read())
    assert body == {"type": "error", "error": {"type": "authentication_error", "message": "omm cluster: missing or invalid token"}}
    assert upstream.calls == 0


def test_revoked_token_is_rejected_immediately(gateway_server, upstream, token):
    assert post(gateway_server, "/v1/messages", headers={"Authorization": f"Bearer {token}"}).status == 200
    tokens.revoke_token("kim")
    assert post(gateway_server, "/v1/messages", headers={"Authorization": f"Bearer {token}"}).status == 401


@pytest.mark.parametrize("method,path", [("POST", "/instance"), ("GET", "/state"), ("DELETE", "/instance/x"),
                                         ("POST", "/models/add"), ("GET", "/v1/messages"), ("GET", "/node_id")])
def test_admin_paths_are_404_even_with_a_valid_token(gateway_server, upstream, token, method, path):
    resp = request(gateway_server, method, path, headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 404
    assert json.loads(resp.read())["error"]["type"] == "not_found_error"
    assert upstream.calls == 0


def test_oversized_body_is_413(gateway_server, upstream, token):
    conn = http.client.HTTPConnection("127.0.0.1", gateway_server.server_address[1], timeout=10)
    conn.putrequest("POST", "/v1/messages")
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Length", str(16 * 1024 * 1024 + 1))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    assert json.loads(resp.read())["error"]["message"] == "omm cluster: request body is larger than 16 MiB"
    assert upstream.calls == 0


def test_upstream_down_is_502_api_error(gateway_without_upstream, token):
    resp = post(gateway_without_upstream, "/v1/messages", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 502
    assert json.loads(resp.read())["error"] == {"type": "api_error", "message": "omm cluster: exo is not running on this Mac"}


def test_log_records_contain_no_body_or_token(gateway_server, token, caplog):
    caplog.set_level(logging.DEBUG, logger="omm.cluster.gateway")
    post(gateway_server, "/v1/messages", headers={"Authorization": f"Bearer {token}"}, body=b'{"secret":"payroll"}').read()
    assert "payroll" not in caplog.text and token not in caplog.text
    import time

    for _ in range(100):
        records = [r for r in caplog.records if r.name == "omm.cluster.gateway"]
        if records:
            break
        time.sleep(0.02)
    record = records[0]
    assert record.token_name == "kim" and record.status == 200 and record.path == "/v1/messages"


def test_claude_system_role_messages_move_to_top_level_system():
    body = json.dumps({"model": "m", "system": "base", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "reminder A"},
        {"role": "system", "content": [{"type": "text", "text": "reminder B"}]},
        {"role": "assistant", "content": "ok"}]}).encode()
    fixed = json.loads(gateway.normalize_claude_messages(body))
    assert [m["role"] for m in fixed["messages"]] == ["user", "assistant"]
    assert fixed["system"] == [{"type": "text", "text": "base"}, {"type": "text", "text": "reminder A"},
                               {"type": "text", "text": "reminder B"}]


def test_normalize_leaves_clean_or_invalid_bodies_byte_identical():
    clean = b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'
    assert gateway.normalize_claude_messages(clean) is clean
    assert gateway.normalize_claude_messages(b"not json") == b"not json"
    assert gateway.normalize_claude_messages(b"") == b""


def test_gateway_rewrites_system_role_only_for_messages_route(gateway_server, upstream, token):
    body = json.dumps({"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}).encode()
    post(gateway_server, "/v1/messages?beta=true", headers={"Authorization": f"Bearer {token}"}, body=body).read()
    sent = json.loads(upstream.last_body)
    assert sent["system"] == [{"type": "text", "text": "s"}] and [m["role"] for m in sent["messages"]] == ["user"]
    assert int(upstream.last_headers["Content-Length"]) == len(upstream.last_body)
    post(gateway_server, "/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, body=body).read()
    assert upstream.last_body == body
