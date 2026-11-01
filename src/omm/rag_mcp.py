"""Local document search as a Model Context Protocol (MCP) stdio server.

A deliberately small JSON-RPC 2.0 implementation of the MCP subset coding
agents need: ``initialize``, ``ping``, ``tools/list``, ``tools/call`` and
notifications. Messages are newline-delimited UTF-8 JSON on stdin/stdout, as
the MCP stdio transport specifies. stdout carries protocol messages only;
diagnostics go to stderr and never include questions or document text.

Run with ``python -m omm.rag_mcp`` (what ``omm rag connect`` registers for a
Python install) or ``omm rag mcp``. Single user, this PC only: there is no
network listener. The cluster gateway's ``/mcp`` route (``omm.rag_http``)
reuses :class:`Server` with an adapter that only sees the caller's collections.
"""
from __future__ import annotations

import json
import sys

from omm import lang

SERVER_NAME = "omm-documents"
# Newest first. A client asking for one of these gets it back; any other
# requested version gets our newest, and the client decides whether to go on.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_STRUCTURED_SINCE = "2025-06-18"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

INSTRUCTIONS = (
    "Searches documents the user registered in OMM on this computer. "
    "Quote results exactly and cite document name and page. "
    "When found is false there is no supporting evidence: say so instead of guessing."
)

TOOLS = [
    {
        "name": "search_documents",
        "title": "Search local documents",
        "description": (
            "Search the user's documents registered in OMM on this computer and return exact quotations "
            "with document name and page. If nothing supports the query the result says 'No evidence' "
            "and found is false; do not answer from memory in that case."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or key words. Use words that appear in the documents."},
                "collection": {"type": "string", "description": "Optional collection name from list_collections."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Maximum results (default 5)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_collections",
        "title": "List local document collections",
        "description": "List document collections (or registered documents when collections are unavailable) in OMM on this computer.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def _version() -> str:
    try:
        from omm import package_metadata
        return str(package_metadata.version() or "unknown")
    except Exception:
        return "unknown"


def _log(message: str) -> None:
    # Fixed diagnostic strings only: never questions, arguments or document text.
    print(f"omm rag mcp: {message}", file=sys.stderr, flush=True)


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _search_text(result: dict) -> str:
    if not result["found"]:
        lines = [result["message"]]
    else:
        lines = [result["message"]]
        for item in result["results"]:
            lines.append(f"\n[{item['rank']}] {item['document']} p.{item['page']} ({item['method']})")
            lines.append(item["quote"])
    if result.get("skipped_stale"):
        lines.append(lang.t("\n색인을 다시 만들어야 해서 검색하지 않은 문서: ", "\nNot searched because their index must be rebuilt: ")
                     + ", ".join(result["skipped_stale"]))
    return "\n".join(lines)


def _collections_text(result: dict) -> str:
    if not result["documents"]:
        from omm.rag_adapter import no_documents
        return no_documents()
    lines = []
    if result["collections_supported"]:
        lines.append("Collections: " + ", ".join(str(c.get("name") if isinstance(c, dict) else c) for c in result["collections"]))
    else:
        lines.append("Collections are not available yet; search covers all registered documents.")
    lines.extend(f"- {d['name']} ({d['pages']} pages{'' if d['index_current'] else ', needs re-registration'})"
                 for d in result["documents"])
    return "\n".join(lines)


class Server:
    """Transport-independent MCP request handler."""

    def __init__(self, adapter=None, *, instructions: str = INSTRUCTIONS):
        if adapter is None:
            from omm import rag_adapter as adapter
        self.adapter = adapter
        self.instructions = instructions
        self.protocol_version = PROTOCOL_VERSIONS[0]

    # -- JSON-RPC envelope -------------------------------------------------
    def handle_line(self, line: bytes | str) -> str | None:
        """One transport message in, one response line out (None for notifications)."""
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            return self._encode(self._error(None, PARSE_ERROR, "Parse error"))
        if isinstance(message, list):
            responses = [r for r in (self._dispatch(m) for m in message) if r is not None] if message else [
                self._error(None, INVALID_REQUEST, "Empty batch")]
            return self._encode(responses) if responses else None
        response = self._dispatch(message)
        return None if response is None else self._encode(response)

    @staticmethod
    def _encode(value) -> str:
        # Compact JSON never contains a raw newline, as the stdio transport requires.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _error(identifier, code, message):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def _dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # A response to a server request; this server sends none.
            identifier = message.get("id") if isinstance(message, dict) else None
            return self._error(identifier if isinstance(identifier, (str, int)) else None, INVALID_REQUEST, "Invalid Request")
        is_notification = "id" not in message
        identifier = message.get("id")
        if not is_notification and (not isinstance(identifier, (str, int)) or isinstance(identifier, bool)):
            return self._error(None, INVALID_REQUEST, "Invalid Request id")
        params = message.get("params", {})
        if params is None:
            params = {}
        try:
            if not isinstance(params, dict):
                raise ProtocolError(INVALID_PARAMS, "params must be an object")
            result = self._method(message["method"], params)
        except ProtocolError as error:
            return None if is_notification else self._error(identifier, error.code, str(error))
        except Exception as error:  # Never echo internals or document text.
            _log(f"internal error ({type(error).__name__})")
            return None if is_notification else self._error(identifier, INTERNAL_ERROR, "Internal error")
        return None if is_notification else {"jsonrpc": "2.0", "id": identifier, "result": result}

    # -- MCP methods -------------------------------------------------------
    def _method(self, method: str, params: dict):
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params)
        raise ProtocolError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict):
        requested = params.get("protocolVersion")
        self.protocol_version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "OMM local documents", "version": _version()},
            "instructions": self.instructions,
        }

    def _call(self, params: dict):
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ProtocolError(INVALID_PARAMS, "arguments must be an object")
        if name == "search_documents":
            unknown = set(arguments) - {"query", "collection", "limit"}
            if unknown:
                return self._tool_error("Unknown argument: " + ", ".join(sorted(unknown)))
            run, render = (lambda: self.adapter.search(arguments.get("query"), arguments.get("collection"), arguments.get("limit"))), _search_text
        elif name == "list_collections":
            run, render = self.adapter.list_collections, _collections_text
        else:
            raise ProtocolError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            result = run()
        except ValueError as error:
            # SearchError and DocumentStore errors are written for the user
            # and contain no document text.
            return self._tool_error(str(error))
        response = {"content": [{"type": "text", "text": render(result)}], "isError": False}
        if self.protocol_version >= _STRUCTURED_SINCE:
            response["structuredContent"] = result
        return response

    @staticmethod
    def _tool_error(message: str):
        return {"content": [{"type": "text", "text": message}], "isError": True}


def serve(stdin=None, stdout=None, server: Server | None = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    # Anything else that prints must not corrupt the protocol stream.
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    # Agents read the replies; they get English like the rest of the CLI.
    language = lang.use(lang.ENGLISH)
    server = server or Server()
    _log("ready (local documents, stdio)")
    try:
        for line in stdin:
            if not line.strip():
                continue
            reply = server.handle_line(line)
            if reply is not None:
                stdout.write(reply.encode("utf-8") + b"\n")
                stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = real_stdout
        lang.reset(language)
    _log("stopped")
    return 0


def main() -> None:
    raise SystemExit(serve())


if __name__ == "__main__":
    main()
