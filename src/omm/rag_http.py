"""Shared document search over the cluster gateway (#504).

The gateway (``omm.cluster.gateway``) checks the employee token first and
hands requests for ``/mcp`` and ``/rag/v1/...`` to :func:`handle` together
with the verified token name. Everything runs on the head Mac against its
own document store; embeddings use its local Ollama.

* ``POST /mcp`` is the MCP Streamable HTTP transport with plain JSON
  responses (no SSE stream, no sessions), sharing the request handler with
  the stdio server in :mod:`omm.rag_mcp`.
* ``/rag/v1/...`` is a small management API for ``omm rag`` on an
  employee's PC.

Collection and document names travel in the query string, request body or
headers, never in the path, because the gateway's access log records the
path. Nothing here logs questions, names or document text.
"""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from omm import documents, rag_acl, rag_mcp, rag_readiness

MAX_JSON_BODY = 64 * 1024
# Imports embed on the head and can take minutes; a few at once is plenty
# for an office and keeps one employee from occupying every worker.
_SLOTS = threading.BoundedSemaphore(4)
REMOTE_INSTRUCTIONS = (
    "Searches company documents shared with you on the OMM cluster head. "
    "Quote results exactly and cite document name and page. "
    "When found is false there is no supporting evidence: say so instead of guessing."
)
_JSON = {"Content-Type": "application/json"}


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _error(status: int, kind: str, message: str):
    return status, dict(_JSON), _encode({"type": "error", "error": {"type": kind, "message": message}})


def _ok(value, status: int = 200):
    return status, dict(_JSON), _encode(value)


_KINDS = {400: "invalid_request_error", 403: "permission_error", 404: "not_found_error", 409: "conflict_error",
          413: "invalid_request_error", 429: "rate_limit_error", 500: "api_error"}


def handle(method: str, raw_path: str, headers, body: bytes, name: str):
    """Return (status, headers, body bytes) for one verified request."""
    parts = urlsplit(raw_path)
    try:
        caller = rag_acl.employee(name)
    except rag_acl.AccessError:
        return _error(403, "permission_error", "omm rag: this token name cannot use shared documents")
    if not _SLOTS.acquire(blocking=False):
        status, extra, payload = _error(429, "rate_limit_error", "omm rag: the head is busy; try again shortly")
        return status, {**extra, "Retry-After": "5"}, payload
    try:
        if parts.path == "/mcp":
            return _mcp(method, headers, body, caller)
        return _rest(method, parts.path, parse_qs(parts.query), headers, body, caller)
    except rag_acl.AccessError as error:
        return _error(error.status, _KINDS.get(error.status, "invalid_request_error"), str(error))
    except ValueError as error:
        # Document store messages are written for the user and hold no document text.
        return _error(400, "invalid_request_error", str(error))
    except Exception:
        return _error(500, "api_error", "omm rag: the head hit an internal error")
    finally:
        _SLOTS.release()


# -- MCP Streamable HTTP ---------------------------------------------------

def _rpc_error(status: int, code: int, message: str):
    return status, dict(_JSON), _encode({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})


def _origin_ok(headers) -> bool:
    origin = headers.get("Origin")
    if not origin:
        return True  # Agents are not browsers and send no Origin.
    return urlsplit(origin).netloc.casefold() == (headers.get("Host") or "").casefold()


def _mcp(method, headers, body, caller):
    if method != "POST":
        # No server-initiated stream and no sessions to end.
        status, extra, payload = _rpc_error(405, rag_mcp.INVALID_REQUEST, "Method not allowed; POST JSON-RPC messages")
        return status, {**extra, "Allow": "POST"}, payload
    if not _origin_ok(headers):
        return _rpc_error(403, rag_mcp.INVALID_REQUEST, "Origin not allowed")
    version = headers.get("MCP-Protocol-Version")
    if version is not None and version not in rag_mcp.PROTOCOL_VERSIONS:
        return _rpc_error(400, rag_mcp.INVALID_REQUEST, "Unsupported MCP-Protocol-Version")
    if len(body) > MAX_JSON_BODY:
        return _rpc_error(413, rag_mcp.INVALID_REQUEST, "Message too large")
    server = rag_mcp.Server(rag_acl.ScopedAdapter(caller), instructions=REMOTE_INSTRUCTIONS)
    # Without the header the transport spec says to assume 2025-03-26;
    # initialize negotiates its own version from the request.
    server.protocol_version = version or "2025-03-26"
    reply = server.handle_line(body)
    if reply is None:
        return 202, {}, b""
    return 200, dict(_JSON), reply.encode("utf-8")


# -- management API --------------------------------------------------------

def _json_body(body: bytes) -> dict:
    if len(body) > MAX_JSON_BODY:
        raise rag_acl.AccessError("요청이 너무 커요.", 413)
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeError):
        raise rag_acl.AccessError("요청 본문은 JSON 객체여야 해요.") from None
    if not isinstance(data, dict):
        raise rag_acl.AccessError("요청 본문은 JSON 객체여야 해요.")
    return data


def _one(query: dict, key: str, required=True):
    values = query.get(key)
    if not values:
        if required:
            raise rag_acl.AccessError(f"'{key}' 값이 필요해요.")
        return None
    if len(values) != 1:
        raise rag_acl.AccessError(f"'{key}' 값은 하나만 주세요.")
    return values[0]


def _collection_view(row) -> dict:
    view = {"name": row["name"], "owner": row["owner"], "documents": row["documents"],
            "created_at": row["created_at"], "can_manage": row["can_manage"]}
    if row["shares"] is not None:
        view["shares"] = row["shares"]
    return view


def _document_view(row) -> dict:
    return {key: row.get(key) for key in ("name", "collection", "pages", "chunks", "size_bytes", "imported_at",
                                          "index_current", "semantic", "reindex_needed")}


def _rest(method, path, query, headers, body, caller):
    route = (method, path)
    if route == ("GET", "/rag/v1/whoami"):
        return _ok({"name": caller.name})
    if route == ("GET", "/rag/v1/readiness"):
        # State and model only: fix commands and paths are for the head's administrator.
        return _ok(rag_readiness.public(rag_readiness.check()))
    if route == ("GET", "/rag/v1/collections"):
        return _ok({"collections": [_collection_view(row) for row in rag_acl.collections(caller)]})
    if route == ("POST", "/rag/v1/collections"):
        created = rag_acl.create_collection(caller, _json_body(body).get("name"))
        return _ok({"name": created["name"], "owner": created["owner"]}, 201)
    if route == ("DELETE", "/rag/v1/collections"):
        result = rag_acl.delete_collection(caller, _one(query, "collection"))
        return _ok({"removed": True, "name": result["name"], "documents": result["documents"]})
    if route == ("GET", "/rag/v1/documents"):
        rows = rag_acl.documents_in(caller, _one(query, "collection"))
        return _ok({"documents": [_document_view(row) for row in rows]})
    if route == ("POST", "/rag/v1/documents"):
        collection = _one(query, "collection")
        raw_name = headers.get("X-OMM-Filename") or ""
        try:
            filename = unquote(raw_name, errors="strict")
        except UnicodeError:
            raise rag_acl.AccessError("문서 파일 이름을 확인해 주세요.") from None
        if len(body) > documents.MAX_UPLOAD:
            raise rag_acl.AccessError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.", 413)
        replace = (headers.get("X-OMM-Replace") or "") == "1"
        result = rag_acl.import_document(caller, collection, filename, body, replace=replace)
        keep = ("id", "name", "collection", "chunks", "sha256", "unchanged", "semantic", "semantic_notice")
        return _ok({key: result[key] for key in keep if key in result}, 201)
    if route == ("DELETE", "/rag/v1/documents"):
        result = rag_acl.remove_document(caller, _one(query, "collection"), _one(query, "name"))
        return _ok({"removed": True, "name": result["name"], "collection": result["collection"]})
    if route == ("POST", "/rag/v1/search"):
        data = _json_body(body)
        unknown = set(data) - {"query", "collection", "limit"}
        if unknown:
            raise rag_acl.AccessError("알 수 없는 검색 값: " + ", ".join(sorted(unknown)))
        return _ok(rag_acl.ScopedAdapter(caller).search(data.get("query"), data.get("collection"), data.get("limit")))
    if route == ("GET", "/rag/v1/shares"):
        return _ok(rag_acl.shares(caller, _one(query, "collection")))
    if route in (("POST", "/rag/v1/shares"), ("DELETE", "/rag/v1/shares")):
        if method == "POST":
            data = _json_body(body)
            return _ok(rag_acl.share(caller, data.get("collection"), person=data.get("person"), team=data.get("team")))
        return _ok(rag_acl.unshare(caller, _one(query, "collection"), person=_one(query, "person", False),
                                   team=_one(query, "team", False)))
    return _error(404, "not_found_error", "omm rag: this path is not served by the gateway")
