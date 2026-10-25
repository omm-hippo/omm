"""The only cluster port employees' PCs can reach.

exo's API listens on every interface with no authentication, and the pf
anchor keeps it off the office network. This server sits on the head Mac,
checks a per-employee token, and forwards only the inference routes to
exo on loopback. Bodies are streamed straight through and never logged.
"""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import time
from urllib.parse import urlsplit, urlunsplit

from omm import config
from omm.cluster.tokens import TokenStore

GATEWAY_PORT = 52440
UPSTREAM = "http://127.0.0.1:52415"
MAX_BODY_BYTES = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = 600
# Per socket operation, not per request: a streamed answer can run for many
# minutes, but a client that sends nothing for this long is gone (or slow-
# loris) and must not hold a thread.
CLIENT_TIMEOUT = 60
# The gateway runs for weeks and logs one line per request, so it keeps its
# own bounded access log instead of growing one run-log file forever.
ACCESS_LOG_MAX_BYTES = 5 * 1024 * 1024
ACCESS_LOG_BACKUPS = 3
# Paths every supported harness was seen calling on a real exo 1.0.71 node
# (spike findings §6). Instance, model-management and state routes stay closed.
ALLOWED_ROUTES = frozenset({
    ("POST", "/v1/messages"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "authorization", "x-api-key", "content-length",
})
# send_response() writes these itself; copying exo's as well sent them twice.
_SET_BY_GATEWAY = frozenset({"date", "server"})
_ACCESS_FIELDS = ("method", "path", "status", "token_name", "duration_ms")

log = logging.getLogger("omm.cluster.gateway")


def _error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


def _text_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def normalize_claude_messages(body: bytes) -> bytes:
    """Fold role "system" messages into the top-level `system` field.

    Claude Code sends some reminders as system-role entries inside `messages`;
    exo's Messages API accepts only user/assistant there and answers 422
    (spike findings §6). Anything that is not such a request passes through
    unchanged, byte for byte.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return body
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return body
    system = _text_blocks(data.get("system"))
    kept = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            system.extend(_text_blocks(message.get("content")))
        else:
            kept.append(message)
    data["system"] = system
    data["messages"] = kept
    return json.dumps(data).encode("ascii")


class _AccessFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)), "level": record.levelname}
        payload.update({key: record.__dict__[key] for key in _ACCESS_FIELDS if key in record.__dict__})
        return json.dumps(payload, ensure_ascii=False, default=str)


def attach_access_log() -> logging.Handler:
    """Send request lines to OMM_HOME/logs/cluster-gateway-access.jsonl (rotated), not the run log."""
    from logging.handlers import RotatingFileHandler

    path = config.OMM_HOME / "logs" / "cluster-gateway-access.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=ACCESS_LOG_MAX_BYTES, backupCount=ACCESS_LOG_BACKUPS,
                                  encoding="utf-8")
    handler.setFormatter(_AccessFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handler


def detach_access_log(handler: logging.Handler) -> None:
    log.removeHandler(handler)
    handler.close()
    log.propagate = True


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str = "0.0.0.0", port: int = GATEWAY_PORT, *, upstream: str = UPSTREAM,
                 store: TokenStore | None = None) -> None:
        parsed = urlsplit(upstream)
        self.upstream_host = parsed.hostname or "127.0.0.1"
        self.upstream_port = parsed.port or 80
        self.store = store or TokenStore()
        super().__init__((host, port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_TIMEOUT
    server: GatewayServer

    def log_message(self, *_args) -> None:
        pass

    def _presented_token(self) -> str:
        auth = self.headers.get("Authorization") or ""
        if auth[:7].casefold() == "bearer ":
            return auth[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _reply_error(self, status: int, kind: str, message: str) -> None:
        body = _error_body(kind, message)
        self._replied = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _handle(self) -> None:
        started = time.monotonic()
        path = urlsplit(self.path).path
        self._replied = False
        try:
            status, name = self._dispatch(path)
        except Exception:
            status, name = 500, None
            if not self._replied:
                self._reply_error(500, "api_error", "omm cluster: the gateway hit an internal error")
            self.close_connection = True
        log.info(
            "%s %s -> %s", self.command, path, status,
            extra={"event": "cluster_gateway", "method": self.command, "path": path, "status": status,
                   "token_name": name, "duration_ms": round((time.monotonic() - started) * 1000)},
        )

    def _dispatch(self, path: str) -> tuple[int, str | None]:
        name = self.server.store.verify(self._presented_token())
        if name is None:
            self._reply_error(401, "authentication_error", "omm cluster: missing or invalid token")
            return 401, None
        # Shared document search (#504) is answered here, not by exo.
        documents = path == "/mcp" or path.startswith("/rag/")
        if not documents and (self.command, path) not in ALLOWED_ROUTES:
            self._reply_error(404, "not_found_error", "omm cluster: this path is not served by the gateway")
            return 404, name
        if self.headers.get("Transfer-Encoding"):
            # Only a Content-Length body is read; the chunks of any other
            # would be parsed as the next request on this connection.
            self._reply_error(411, "invalid_request_error", "omm cluster: send the request body with a Content-Length")
            return 411, name
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply_error(413, "invalid_request_error", "omm cluster: request body is larger than 16 MiB")
            return 413, name
        body = self.rfile.read(length) if length else None
        if documents:
            from omm import rag_http
            status, headers, payload = rag_http.handle(self.command, self.path, self.headers, body or b"", name)
            self._replied = True
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return status, name
        if body and path == "/v1/messages":
            body = normalize_claude_messages(body)
        headers = {k: v for k, v in self.headers.items() if k.casefold() not in _HOP_BY_HOP}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        upstream = http.client.HTTPConnection(self.server.upstream_host, self.server.upstream_port,
                                              timeout=UPSTREAM_TIMEOUT)
        try:
            try:
                # The checked path, not the raw target: an absolute-form
                # "http://host/v1/messages" would otherwise reach exo as typed.
                target = urlunsplit(("", "", path, urlsplit(self.path).query, ""))
                upstream.request(self.command, target, body=body, headers=headers)
                response = upstream.getresponse()
            except TimeoutError:
                self._reply_error(504, "api_error", "omm cluster: exo did not answer in time")
                return 504, name
            except OSError:
                self._reply_error(502, "api_error", "omm cluster: exo is not running on this Mac")
                return 502, name
            self._replied = True
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.casefold() not in _HOP_BY_HOP and key.casefold() not in _SET_BY_GATEWAY:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return response.status, name
        finally:
            upstream.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def run_gateway() -> int:
    handler = attach_access_log()
    server = GatewayServer()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        detach_access_log(handler)
    return 0
