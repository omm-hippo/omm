"""Thin adapter between agent-facing tools (MCP) and the local document store.

The MCP server talks only to this module so the retrieval backend can change
(collections, embeddings, hybrid scoring) without touching the protocol code.
Everything here stays on this machine: no network calls, no logging of
questions or document text.
"""
from __future__ import annotations

import inspect

from omm import documents
from omm.lang import t

DEFAULT_LIMIT = 5
MAX_LIMIT = 10
MAX_QUERY = 2000
# DocumentStore.search accepts at most this many document ids per call.
_SEARCH_BATCH = 20
def no_documents() -> str:
    return t("등록된 문서가 없어요. `omm rag add <컬렉션> <파일이나 폴더>` 또는 omm web의 문서 탭에서 "
             "문서를 먼저 등록해 주세요.",
             "No documents are registered. Register some first with `omm rag add <collection> <file or folder>` "
             "or the Documents tab in omm web.")


class SearchError(ValueError):
    """A user-correctable problem; the message never contains document text."""


def _store():
    return documents.DocumentStore()


def _accepts(function, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def collections_supported(store=None) -> bool:
    """True once the store can list and search named collections."""
    store = store or _store()
    return callable(getattr(store, "collections", None)) and _accepts(store.search, "collection")


def _method(row) -> str:
    return row.get("method") or row.get("retrieval") or documents.RETRIEVAL_METHOD


def _result(row, rank: int) -> dict:
    return {
        "rank": rank,
        "document": row["name"],
        "page": row["page"],
        "quote": row["text"],
        "start": row["start"],
        "end": row["end"],
        "sha256": row.get("sha256"),
        "collection": row.get("collection"),
        "score": row.get("score"),
        "method": _method(row),
    }


def _validate(query, collection, limit):
    if not isinstance(query, str) or not query.strip():
        raise SearchError("query must be a non-empty string.")
    if len(query) > MAX_QUERY:
        raise SearchError(f"query must be at most {MAX_QUERY} characters.")
    if collection is not None and (not isinstance(collection, str) or not collection.strip()):
        raise SearchError("collection must be a non-empty string when given.")
    if limit is None:
        limit = DEFAULT_LIMIT
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise SearchError(f"limit must be an integer from 1 to {MAX_LIMIT}.")
    return query.strip(), (collection.strip() if collection else None), limit


def search(query, collection=None, limit=None, *, store=None) -> dict:
    """Search registered documents and return exact quotations.

    ``found`` is False with an explicit message when nothing supports the
    question; callers must not present that as a successful empty answer.
    """
    query, collection, limit = _validate(query, collection, limit)
    store = store or _store()
    if collection is not None and not collections_supported(store):
        raise SearchError("This OMM version has no document collections yet; omit collection to search all registered documents.")
    if collection is not None and collection not in {row.get("name") for row in store.collections()}:
        raise SearchError(f"No document collection named {collection!r}; call list_collections to see the available names.")
    rows = store.list()
    if collection is not None:
        rows = [row for row in rows if row.get("collection") == collection]
    current = [row for row in rows if row.get("index_current", True)]
    stale = sorted(row["name"] for row in rows if not row.get("index_current", True))
    base = {"collection": collection, "skipped_stale": stale,
            "searched_documents": len(current)}
    if not current:
        message = no_documents() if not rows else t(
            "검색할 수 있는 문서가 없어요. 색인 방식이 바뀐 문서는 원본을 다시 등록해 주세요.",
            "No searchable documents. Register the originals again for documents whose index format changed.")
        return {**base, "found": False, "message": message, "results": []}
    ids = [row["id"] for row in current]
    kwargs = {"collection": collection} if collection is not None else {}
    batches, methods, notices = [], set(), []
    for offset in range(0, len(ids), _SEARCH_BATCH):
        chunk = ids[offset:offset + _SEARCH_BATCH]
        if callable(getattr(store, "search_detailed", None)):
            detail = store.search_detailed(query, chunk, limit=limit, **kwargs)
            sources = detail["sources"]
            methods.add(detail.get("method"))
            if detail.get("notice") and detail["notice"] not in notices:
                notices.append(detail["notice"])
        else:
            _, sources = store.search(query, chunk, **kwargs)
        batches.append(list(sources))
    methods.discard(None)
    if methods:
        # One lexical batch means the answer as a whole was not fully semantic.
        base["method"] = "hybrid" if methods == {"hybrid"} else "lexical"
        base["notice"] = " ".join(notices) or None
    # Each batch is already ranked. Without comparable scores across batches,
    # interleave by in-batch rank; with scores, order by score.
    merged = [batch[depth] for depth in range(max(map(len, batches), default=0))
              for batch in batches if depth < len(batch)]
    if all(isinstance(row.get("score"), (int, float)) for row in merged):
        merged.sort(key=lambda row: -row["score"])
    results = [_result(row, index + 1) for index, row in enumerate(merged[:limit])]
    if not results:
        return {**base, "found": False, "message": t("근거 없음: ", "No evidence: ") + documents.no_evidence(), "results": []}
    return {**base, "found": True, "message": t(f"{len(results)}개 근거를 찾았어요.", f"Found {len(results)} supporting passage(s)."),
            "results": results}


def list_collections(*, store=None) -> dict:
    """Collections when supported; otherwise the registered documents."""
    store = store or _store()
    rows = store.list()
    listed = [{"name": row["name"], "pages": row.get("pages"), "chunks": row.get("chunks"),
               "index_current": row.get("index_current", True), "collection": row.get("collection")}
              for row in rows]
    if collections_supported(store):
        return {"collections_supported": True, "collections": list(store.collections()), "documents": listed}
    return {"collections_supported": False,
            "collections": [{"name": None, "description": "All registered documents (collections are not available yet).",
                             "documents": len(rows)}],
            "documents": listed}
