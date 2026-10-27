"""The only cluster port employees' PCs can reach.

exo's API listens on every interface with no authentication, and the pf
anchor keeps it off the office network. This server sits on the head Mac,
checks a per-employee token, and forwards only the inference routes to
exo on loopback. Bodies are streamed straight through and never logged;
only the model list and exo's "No instance found" answer are rewritten.

It speaks HTTPS only, with a certificate from the head's own CA (tls.py,
#536): a plain HTTP request is answered with a pointer to https before a
single byte of it is parsed, so a token sent in the clear is never accepted.
"""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

from datetime import datetime, timezone
import hashlib

from omm import config
from omm.cluster import exo_client, member_auth, members, tls
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
# Answered by the gateway itself: exo's /v1/models lists every model it knows
# (120 on exo 1.0.71), so `omm cluster run` on an employee PC asks here which
# ones are actually loaded. Only model ids, sizes, Mac counts and load state leave /state.
LOADED_MODELS_ROUTE = ("GET", "/omm/loaded-models")
# Read-only views an employee PC asks for (`omm cluster status/recommend/
# search/info`). None of them downloads, loads or unloads anything, and none
# names a Mac: status and info give counts only.
VIEW_ROUTES = frozenset({"/omm/status", "/omm/recommend", "/omm/search", "/omm/info"})
# Other cluster Macs, never employees (member_auth.py, member_link.py).
MEMBER_PREFIX = "/omm/member/"
MEMBER_POLL_ROUTE = ("POST", "/omm/member/poll")
MEMBER_SNAPSHOT_ROUTE = ("GET", "/omm/member/rag/snapshot")
MAX_REPORT_BYTES = 64 * 1024
HANDOVER_MESSAGE = "omm cluster: the head is moving to another Mac; try again in a few minutes"
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "authorization", "x-api-key", "content-length",
})
# send_response() writes these itself; copying exo's as well sent them twice.
_SET_BY_GATEWAY = frozenset({"date", "server"})
_ACCESS_FIELDS = ("method", "path", "status", "token_name", "duration_ms")
# Every TLS connection starts with a handshake record (content type 22).
_TLS_HANDSHAKE = b"\x16"
PLAINTEXT_MESSAGE = ("omm cluster: this gateway only accepts HTTPS. On your PC run: "
                     "omm rag remote set https://<head address>:52440")

log = logging.getLogger("omm.cluster.gateway")


def _error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


def _not_loaded_message(body: bytes | None) -> str:
    try:
        model = json.loads(body or b"").get("model")
    except (ValueError, AttributeError):
        model = None
    named = isinstance(model, str) and model
    return (f"omm cluster: {model if named else 'the requested model'} is not loaded on the cluster. "
            f"The admin loads it on the head Mac with: omm cluster load {model if named else '<model id>'}")


def loaded_models(catalog: bytes, state: bytes) -> bytes:
    """exo's /v1/models body cut down to the models with a ready instance.

    exo lists its whole catalog there (about 120 models on exo 1.0.71), and a
    harness that picks an unloaded one gets "No instance found" (#531).
    """
    data = json.loads(catalog)
    state = json.loads(state)
    rows = data.get("data") if isinstance(data, dict) else None
    if isinstance(rows, list):
        data["data"] = [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
                        and exo_client.instance_ready(state, row["id"])]
    return json.dumps(data).encode()


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


def _plaintext_reply() -> bytes:
    body = _error_body("invalid_request_error", PLAINTEXT_MESSAGE)
    return (b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nConnection: close\r\n"
            b"Content-Length: %d\r\n\r\n%s" % (len(body), body))


def _close_after_draining(sock) -> None:
    """Half-close, discard what the peer still sends (1 s at most), then close."""
    try:
        # On a TLS socket this drops TLS first, so the reads below see raw bytes.
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(1)
        for _ in range(64):
            if not sock.recv(65536):
                break
    except (OSError, ValueError):
        pass
    sock.close()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str = "0.0.0.0", port: int = GATEWAY_PORT, *, upstream: str = UPSTREAM,
                 store: TokenStore | None = None, certificates: tls.HeadCertificates | None = None,
                 member_key: bytes | None = None) -> None:
        parsed = urlsplit(upstream)
        self.upstream_host = parsed.hostname or "127.0.0.1"
        self.upstream_port = parsed.port or 80
        self.store = store or TokenStore()
        self.certificates = certificates or tls.HeadCertificates()
        if member_key is None:
            from omm.cluster import settings

            current = settings.load()
            member_key = member_auth.member_key(current.namespace) if current is not None else None
        self.member_key = member_key
        self.nonces = member_auth.NonceCache()
        super().__init__((host, port), _Handler)

    def finish_request(self, request, client_address) -> None:
        # Runs on the connection's own thread, so a slow handshake never holds accept().
        # The handler's own idle limit also bounds the first byte and the handshake.
        request.settimeout(self.RequestHandlerClass.timeout)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first != _TLS_HANDSHAKE:
            self._refuse_plaintext(request)
            return
        try:
            connection = self.certificates.context().wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            super().finish_request(connection, client_address)
        except (ConnectionError, ssl.SSLError):
            # A client that hangs up mid-request (or only checked the
            # certificate, as `omm rag remote set` does) is not an error.
            pass
        finally:
            # A reply that refuses a request (401, 413, ...) leaves its body
            # unread; TLS sends it as its own record, so closing at once would
            # reset the connection before the client reads the answer.
            _close_after_draining(connection)

    def _refuse_plaintext(self, request) -> None:
        try:
            request.sendall(_plaintext_reply())
        except OSError:
            pass
        # What the client sent is dropped unparsed, token included.
        _close_after_draining(request)
        log.info("plain HTTP -> 400", extra={"event": "cluster_gateway", "method": "PLAINTEXT", "path": None,
                                             "status": 400, "token_name": None, "duration_ms": 0})


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

    def _reply_buffered(self, status: int, upstream_headers, body: bytes) -> None:
        self._replied = True
        self.send_response(status)
        for key, value in upstream_headers:
            if key.casefold() not in _HOP_BY_HOP and key.casefold() not in _SET_BY_GATEWAY:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if (self.command, path) == ("GET", tls.CA_ROUTE):
            # Public by design: an employee PC fetches it before it has trusted
            # anything, and shows its fingerprint to compare with the head's.
            return self._reply_ca(), None
        if path.startswith(MEMBER_PREFIX):
            return self._member(path)
        name = self.server.store.verify(self._presented_token())
        if name is None:
            self._reply_error(401, "authentication_error", "omm cluster: missing or invalid token")
            return 401, None
        if (self.command, path) == LOADED_MODELS_ROUTE:
            return self._reply_loaded_models(), name
        if self.command == "GET" and path in VIEW_ROUTES:
            return self._reply_view(path), name
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
        if documents and members.pending_handover() is not None:
            # The store moves to the new head; a write accepted now would be lost.
            self._reply_error(503, "overloaded_error", HANDOVER_MESSAGE)
            return 503, name
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
                if path == "/v1/models" and response.status == 200:
                    catalog = response.read()
                    upstream.request("GET", "/state", headers={"Accept": "application/json"})
                    state = upstream.getresponse()
                    state_status, state_body = state.status, state.read()
            except TimeoutError:
                self._reply_error(504, "api_error", "omm cluster: exo did not answer in time")
                return 504, name
            except OSError:
                self._reply_error(502, "api_error", "omm cluster: exo is not running on this Mac")
                return 502, name
            if path == "/v1/models" and response.status == 200:
                try:
                    if state_status != 200:
                        raise ValueError(state_status)
                    listed = loaded_models(catalog, state_body)
                except ValueError:
                    self._reply_error(502, "api_error", "omm cluster: could not tell which models exo has loaded")
                    return 502, name
                self._reply_buffered(200, response.getheaders(), listed)
                return 200, name
            if response.status == 404:
                # exo answers a catalog model nobody loaded with "No instance
                # found", which does not tell an employee who can fix it (#531).
                # Its 404 bodies are short JSON, so reading one whole is cheap.
                missing = response.read()
                if b"No instance found" in missing:
                    self._reply_error(404, "not_found_error", _not_loaded_message(body))
                else:
                    self._reply_buffered(404, response.getheaders(), missing)
                return 404, name
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

    def _exo(self):
        from omm.cluster.exo_client import ExoClient

        return ExoClient(f"http://{self.server.upstream_host}:{self.server.upstream_port}")

    def _reply_view(self, path: str) -> int:
        from urllib.parse import parse_qs

        from omm.cluster import models
        from omm.cluster.errors import ClusterError
        from omm.cluster.exo_client import ExoUnavailable

        query = parse_qs(urlsplit(self.path).query)
        argument = (query.get("q") or query.get("model") or [""])[0]
        if path in ("/omm/search", "/omm/info") and not argument:
            self._reply_error(400, "invalid_request_error", "omm cluster: name what to look for")
            return 400
        client = self._exo()
        try:
            if path == "/omm/status":
                data = models.employee_status(client)
            elif path == "/omm/recommend":
                data = models.recommend_rows(client)
            elif path == "/omm/search":
                data = models.search(argument, client)
            else:
                data = models.employee_info(argument, client)
        except ExoUnavailable:
            self._reply_error(502, "api_error", "omm cluster: exo is not running on the head Mac")
            return 502
        except ClusterError as error:
            self._reply_error(400, "invalid_request_error", f"omm cluster: {error}")
            return 400
        return self._reply_json(data)

    def _reply_loaded_models(self) -> int:
        from omm.cluster.errors import ClusterError

        try:
            raw = self._exo().raw_state()
        except ClusterError:
            self._reply_error(502, "api_error", "omm cluster: exo is not running on this Mac")
            return 502
        from omm.cluster.models import inventory_from

        rows = [row.as_dict() for row in inventory_from(raw) if row.state in ("loaded", "loading")]
        # "models" stays bare ids for employees on an older omm; "rows" carry the
        # size and Mac count `omm cluster list` shows.
        return self._reply_json({"models": [row["model"] for row in rows], "rows": rows})

    def _member(self, path: str) -> tuple[int, str | None]:
        key = self.server.member_key
        node = None if key is None else member_auth.verify_request(
            key, self.command, path, self.headers, now=time.time(), nonces=self.server.nonces)
        if node is None:
            self._reply_error(401, "authentication_error", "omm cluster: this is not a Mac of this cluster")
            return 401, None
        caller = f"member:{node}"
        nonce = self.headers[member_auth.HEADER_NONCE]
        if (self.command, path) == MEMBER_SNAPSHOT_ROUTE:
            from omm.cluster import rag_backup

            found = rag_backup.snapshot()
            if found is None:
                self._reply_error(404, "not_found_error", "omm cluster: the head has no documents")
                return 404, caller
            file, sha, marker = found
            proof = self._head_proof(nonce, sha)
            self._replied = True
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Content-Length", str(file.stat().st_size))
            self.send_header("X-Omm-Rag-Sha256", sha)
            self.send_header("X-Omm-Rag-Marker", marker)
            self.send_header(member_auth.HEADER_HEAD_PROOF, proof)
            self.end_headers()
            with file.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    self.wfile.write(block)
            self.close_connection = True
            return 200, caller
        if (self.command, path) != MEMBER_POLL_ROUTE:
            self._reply_error(404, "not_found_error", "omm cluster: this path is not served by the gateway")
            return 404, caller
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REPORT_BYTES or self.headers.get("Transfer-Encoding"):
            self._reply_error(413, "invalid_request_error", "omm cluster: member report is too large")
            return 413, caller
        try:
            report = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            report = None
        if not isinstance(report, dict):
            self._reply_error(400, "invalid_request_error", "omm cluster: member report must be a JSON object")
            return 400, caller
        from omm.cluster import rag_backup, settings, version_follow

        members.record(node, report, now=datetime.now(timezone.utc))
        current = settings.load()
        pending = members.pending_handover()
        body = json.dumps({
            "head": True,
            "epoch": current.head_epoch if current is not None else 0,
            "version": version_follow.local_version(),
            "rag_marker": rag_backup.store_marker(),
            "handover": pending if pending is not None and pending.get("to") == node else None,
        }).encode()
        # Before the reply starts, so a failure here still reaches the member as a 500.
        proof = self._head_proof(nonce, hashlib.sha256(body).hexdigest())
        self._replied = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(member_auth.HEADER_HEAD_PROOF, proof)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        return 200, caller

    def _head_proof(self, nonce: str, body_sha256: str) -> str:
        # server.pem holds the chain; the leaf comes first and is what the member sees.
        chain = (self.server.certificates.directory / tls.SERVER_FILE).read_text(encoding="ascii")
        end = "-----END CERTIFICATE-----"
        leaf = member_auth.leaf_sha256(ssl.PEM_cert_to_DER_cert(chain[:chain.index(end) + len(end)]))
        return member_auth.head_proof(self.server.member_key, nonce, leaf, body_sha256)

    def _reply_ca(self) -> int:
        body = self.server.certificates.ca_pem()
        self._replied = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        return 200

    def _reply_json(self, data) -> int:
        body = json.dumps(data).encode()
        self._replied = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # A request body is never read on this route; it must not be parsed as the next request.
        self.close_connection = True
        return 200

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def run_gateway() -> int:
    import threading

    from omm.cluster import autoload, handover

    handler = attach_access_log()
    server = GatewayServer()
    # This service is the head's one always-running process, so it also puts
    # the model back after exo restarts.
    stop_autoload = autoload.start()
    # A head that comes back after a handover must not keep serving employees.
    stop_check = threading.Event()
    threading.Thread(target=handover.stale_head_loop, args=(stop_check,), name="omm-stale-head",
                     daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_autoload.set()
        stop_check.set()
        server.server_close()
        detach_access_log(handler)
    return 0
