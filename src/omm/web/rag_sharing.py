"""내 문서 tab: collections, sharing, teams, employee tokens and agent connections.

The browser only talks to this loopback server. Each call goes either to this
PC's document store (the local administrator, as ``omm rag ...`` without a
remote) or, after ``회사에 연결`` (``omm rag remote set``), to the cluster head
through :mod:`omm.rag_remote`. The employee token stays in
``OMM_HOME/rag-remote.json``: no response, job record or error carries it.
The only token that ever reaches the browser is a newly issued one, once, in
the issue response of the administrator's token panel.

Permission rules and messages are the CLI's (:mod:`omm.rag_acl`,
:mod:`omm.rag_cli`); long uploads run as worker jobs (``document_import``).
"""
from __future__ import annotations

import base64

from omm import documents, rag_acl, rag_remote

HARNESS_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "pi": "pi"}
SEARCH_LIMIT = 5
_KINDS = {"person", "team"}
# Same wording as `omm rag team ...` while a remote head is set.
TEAMS_ON_HEAD = "팀은 헤드 Mac에서만 관리해요. 이 PC는 원격 헤드를 쓰도록 설정돼 있어요(`omm rag remote`)."
TOKENS_ON_HEAD = "직원 출입증(토큰)은 클러스터 헤드에서만 관리해요. 이 PC는 헤드로 설정돼 있지 않아요."


class Problem(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# -- where requests go --------------------------------------------------------

def _remote():
    """The head client when a remote is set, else None. A broken file is reported, never guessed."""
    return rag_remote.client()


def _remote_set() -> bool:
    return rag_remote.remote_path().exists()


def tokens_enabled() -> bool:
    """This PC is a cluster head (or already keeps employee tokens), and no remote head is set."""
    from omm.cluster import settings, tokens
    if _remote_set():
        return False
    current = settings.load()
    return (current is not None and current.role == "head") or tokens.tokens_path().exists()


def _admin_only(message=TEAMS_ON_HEAD):
    if _remote_set():
        raise Problem(message, 403)


def _harnesses(remote_mode: bool) -> list[dict]:
    from omm import rag_connect
    rows = []
    for harness in rag_connect.HARNESSES:
        row = {"harness": harness, "label": HARNESS_LABELS[harness]}
        try:
            status = rag_connect.status(harness)
        except (rag_connect.RagConnectionError, OSError, ValueError) as error:
            row["error"] = str(error) if isinstance(error, rag_connect.RagConnectionError) else "설정 파일을 읽지 못했어요. 권한을 확인해 주세요."
            rows.append(row)
            continue
        # Never the entry itself: a remote entry holds the employee token.
        row.update(installed=status["installed"], registered=status["registered"], managed=status["managed"],
                   remote=status["remote"], current=status["current_command"], path=status["path"])
        rows.append(row)
    return rows


def state() -> dict:
    """Where the tab is looking, who I am there, and which admin panels apply."""
    try:
        remote = rag_remote.load()
    except rag_remote.RemoteError as error:
        return {"mode": "remote", "remote": None, "error": str(error), "admin": False, "tokens": False,
                "harnesses": _harnesses(True)}
    if remote is None:
        return {"mode": "local", "remote": None, "error": None, "admin": True, "tokens": tokens_enabled(),
                "harnesses": _harnesses(False)}
    name, error = None, None
    try:
        name = rag_remote.Client(remote).call("GET", "/rag/v1/whoami", timeout=8)["name"]
    except rag_remote.RemoteError as failure:
        error = str(failure)
    except (KeyError, TypeError):
        error = "헤드의 응답을 읽지 못했어요. 헤드의 omm 버전을 확인해 주세요."
    return {"mode": "remote", "remote": {"url": remote.url, "name": name}, "error": error, "admin": False,
            "tokens": False, "harnesses": _harnesses(True)}


# -- collections and documents --------------------------------------------------

def _collection_view(row, me) -> dict:
    owner = row.get("owner") or rag_acl.ADMIN_OWNER
    if me is None:
        role = "관리자"
    elif owner == me:
        role = "주인"
    else:
        role = "공유받음"
    view = {"name": row["name"], "documents": row.get("documents", 0), "owner": rag_acl.owner_label(owner),
            "mine": me is not None and owner == me, "role": role, "can_manage": bool(row.get("can_manage")),
            "shares": row.get("shares")}
    if me is None:
        view["default"] = row.get("id") == documents.DEFAULT_COLLECTION
    return view


def collections(me: str | None = None) -> dict:
    client = _remote()
    if client is None:
        return {"collections": [_collection_view(row, None) for row in rag_acl.collections(rag_acl.ADMIN)]}
    if me is None:
        me = client.whoami()
    return {"collections": [_collection_view(row, me) for row in client.collections()]}


_DOCUMENT_KEYS = ("id", "name", "collection", "pages", "chunks", "size_bytes", "imported_at",
                  "index_current", "semantic", "reindex_needed")


def documents_in(collection) -> dict:
    _text(collection, "컬렉션을 골라 주세요.")
    client = _remote()
    rows = client.documents(collection) if client else rag_acl.documents_in(rag_acl.ADMIN, collection)
    return {"collection": collection, "documents": [{k: row.get(k) for k in _DOCUMENT_KEYS if k in row} for row in rows]}


def _text(value, message, limit=200):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise Problem(message)
    return value


def _exact(body, keys, message, confirm=False):
    expected = set(keys) | ({"confirmed"} if confirm else set())
    if not isinstance(body, dict) or set(body) != expected:
        raise Problem(message)
    if confirm and body["confirmed"] is not True:
        raise Problem(message)


def upload_request(body) -> dict:
    """Job request for one browser-selected file; the worker reads the remote itself."""
    MAX_UPLOAD = documents.MAX_UPLOAD
    if not isinstance(body, dict) or body.get("confirmed") is not True or set(body) - {"confirmed", "request_id", "collection", "name", "content", "replace"}:
        raise Problem("문서 작업을 확인해 주세요.")
    content, name = body.get("content"), body.get("name")
    collection = _text(body.get("collection"), "문서를 넣을 컬렉션을 골라 주세요.")
    if not isinstance(content, str) or not content or len(content) > ((MAX_UPLOAD + 2) // 3) * 4:
        raise Problem("문서는 6 MiB까지 등록할 수 있어요.")
    if not isinstance(name, str) or not 1 <= len(name) <= 180:
        raise Problem("파일 이름을 확인해 주세요.")
    if type(body.get("replace", False)) is not bool:
        raise Problem("문서 교체 여부를 확인해 주세요.")
    remote = rag_remote.load()
    request = {"operation": "document_import", "filename": name, "content": content,
               "replace": body.get("replace", False), "collection": collection}
    if remote is None:
        # Same rule as `omm rag add` on the head: the administrator manages every collection.
        rag_acl.documents_in(rag_acl.ADMIN, collection)
    else:
        request["remote_url"] = remote.url  # The address only; never the token.
    return request


def run_upload(request, progress) -> dict:
    """Worker side of a ``document_import`` that names a collection."""
    from omm.documents import DocumentStore
    try:
        content = base64.b64decode(request["content"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("파일 전송 내용을 확인하지 못했어요.") from None
    replace = request.get("replace") is True
    if "remote_url" not in request:
        return DocumentStore().import_document(request["filename"], content, replace=replace,
                                               collection=request["collection"], progress=progress)
    client = rag_remote.client()
    if client is None or client.remote.url != request["remote_url"]:
        raise ValueError("연결한 곳이 바뀌었어요. 내 문서 탭을 새로고침한 뒤 다시 올려 주세요.")
    if len(content) > documents.MAX_UPLOAD:
        raise ValueError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.")
    # The head extracts and embeds before it answers; one stage covers both.
    progress("upload", 0, 0)
    result = client.upload(request["collection"], request["filename"], content, replace=replace)
    keep = ("name", "collection", "chunks", "unchanged", "semantic", "semantic_notice")
    return {key: result[key] for key in keep if key in result}


# -- question (search only) ----------------------------------------------------

def search(body) -> dict:
    _exact(body, {"collection", "query"}, "질문과 컬렉션을 정해 주세요.")
    query = body["query"]
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 600:
        raise Problem("질문은 2~600자로 입력해 주세요.")
    collection = _text(body["collection"], "질문할 컬렉션을 골라 주세요.")
    client = _remote()
    if client is not None:
        result = client.search(query.strip(), collection, SEARCH_LIMIT)
    else:
        from omm import rag_adapter
        result = rag_adapter.search(query.strip(), collection, SEARCH_LIMIT)
    keys = ("found", "message", "method", "notice", "collection", "skipped_stale", "searched_documents")
    view = {key: result.get(key) for key in keys}
    view["results"] = [{k: item.get(k) for k in ("rank", "document", "page", "quote", "start", "end", "collection", "method")}
                       for item in result.get("results", [])]
    view["source"] = "remote" if client is not None else "local"
    return view


# -- POST dispatch --------------------------------------------------------------

def _share_target(body):
    kind, target = body.get("kind"), body.get("target")
    if kind not in _KINDS:
        raise Problem("사람이나 팀 중 하나를 골라 주세요.")
    if not isinstance(target, str) or not target.strip() or len(target) > 80:
        raise Problem("공유할 직원 이름(토큰 이름)이나 팀 이름을 적어 주세요.")
    target = target.strip()
    return ({"person": target} if kind == "person" else {"team": target})


def post(path: str, body) -> dict:
    if path == "/api/rag/collections/create":
        _exact(body, {"name"}, "컬렉션 이름을 적어 주세요.")
        client = _remote()
        if client:
            created = client.create_collection(body["name"])
            return {"name": created["name"], "shared": False}
        created = rag_acl.create_collection(rag_acl.ADMIN, body["name"])
        return {"name": created["name"], "shared": False}
    if path == "/api/rag/collections/delete":
        _exact(body, {"name"}, "지울 컬렉션을 확인해 주세요.", confirm=True)
        client = _remote()
        result = client.delete_collection(body["name"]) if client else rag_acl.delete_collection(rag_acl.ADMIN, body["name"])
        return {"name": result["name"], "documents": result["documents"]}
    if path == "/api/rag/documents/remove":
        _exact(body, {"collection", "name"}, "지울 문서를 확인해 주세요.", confirm=True)
        client = _remote()
        if client:
            result = client.remove_document(body["collection"], body["name"])
        else:
            result = rag_acl.remove_document(rag_acl.ADMIN, body["collection"], body["name"])
        return {"name": result["name"], "collection": result["collection"]}
    if path == "/api/rag/search":
        return search(body)
    if path in {"/api/rag/share", "/api/rag/unshare"}:
        removing = path.endswith("unshare")
        _exact(body, {"collection", "kind", "target"}, "공유할 컬렉션과 대상을 확인해 주세요.", confirm=removing)
        target = _share_target(body)
        client = _remote()
        if removing:
            result = client.unshare(body["collection"], **target) if client else rag_acl.unshare(rag_acl.ADMIN, body["collection"], **target)
            return {key: result[key] for key in ("collection", "kind", "target", "removed")}
        result = client.share(body["collection"], **target) if client else rag_acl.share(rag_acl.ADMIN, body["collection"], **target)
        return {key: result[key] for key in ("collection", "kind", "target", "added")}
    if path.startswith("/api/rag/teams/"):
        _admin_only()
        action = path.rsplit("/", 1)[1]
        if action == "create":
            _exact(body, {"name"}, "팀 이름을 적어 주세요.")
            return rag_acl.team_create(body["name"])
        if action == "delete":
            _exact(body, {"name"}, "지울 팀을 확인해 주세요.", confirm=True)
            return rag_acl.team_delete(body["name"])
        if action == "add":
            _exact(body, {"team", "member"}, "팀과 직원 이름(토큰 이름)을 정해 주세요.")
            return rag_acl.team_add(body["team"], body["member"])
        if action == "remove":
            _exact(body, {"team", "member"}, "팀에서 뺄 직원을 확인해 주세요.", confirm=True)
            return rag_acl.team_remove(body["team"], body["member"])
    if path.startswith("/api/rag/tokens/"):
        return _token_post(path.rsplit("/", 1)[1], body)
    if path == "/api/rag/remote/set":
        _exact(body, {"url", "token"}, "회사 head 주소와 직원 토큰을 적어 주세요.")
        if not isinstance(body["url"], str) or not isinstance(body["token"], str) or len(body["url"]) > 300 or len(body["token"]) > 300:
            raise Problem("회사 head 주소와 직원 토큰을 확인해 주세요.")
        # The same checks as `omm rag remote set`: address shape, non-empty token, whoami.
        remote = rag_remote.Remote(rag_remote.normalize_url(body["url"]), body["token"].strip())
        if not remote.token:
            raise rag_remote.RemoteError("토큰이 비어 있어요. 관리자에게 받은 omm_... 토큰을 넣어 주세요.")
        name = rag_remote.Client(remote).whoami()
        try:
            rag_remote.save(remote)
        except OSError:
            raise Problem("설정 파일을 쓰지 못했어요. 권한을 확인해 주세요.", 500) from None
        return {"url": remote.url, "name": name}
    if path == "/api/rag/remote/clear":
        _exact(body, set(), "이 PC로 돌아갈지 확인해 주세요.", confirm=True)
        return {"removed": rag_remote.clear()}
    if path in {"/api/rag/connect", "/api/rag/disconnect"}:
        return _connection(path.endswith("disconnect"), body)
    raise Problem("Unknown operation route", 404)


def _connection(removing, body) -> dict:
    from omm import rag_connect
    if removing:
        _exact(body, {"harness"}, "연결을 해제할 AI 도구를 확인해 주세요.", confirm=True)
    else:
        _exact(body, {"harness"}, "연결할 AI 도구를 골라 주세요.")
    harness = body["harness"]
    if harness not in rag_connect.HARNESSES:
        raise Problem("Claude Code, Codex, Cursor, pi 중에서 골라 주세요.")
    try:
        if removing:
            result = rag_connect.disconnect(harness)
        else:
            # This PC's documents, or the head's after 회사에 연결 (`--remote`).
            result = rag_connect.connect(harness, remote=_remote_set())
    except OSError:
        raise Problem("설정 파일을 읽거나 쓰지 못했어요. 권한을 확인해 주세요.", 500) from None
    return {"harness": harness, "state": result["state"], "remote": (not removing) and _remote_set()}


def _token_post(action, body) -> dict:
    from omm.cluster import tokens
    if not tokens_enabled():
        raise Problem(TOKENS_ON_HEAD, 403)
    try:
        if action == "add":
            _exact(body, {"name"}, "출입증을 받을 직원 이름을 적어 주세요.")
            token = tokens.add_token(body["name"])
            record = next((r for r in tokens.list_tokens() if r.name == body["name"]), None)
            # The one response that carries a token; it cannot be shown again.
            return {"name": body["name"], "token": token, "created_at": record.created_at if record else None}
        if action == "revoke":
            _exact(body, {"name"}, "취소할 출입증을 확인해 주세요.", confirm=True)
            if not tokens.revoke_token(body["name"]):
                raise Problem(f"No token named {body['name']}.", 404)
            return {"name": body["name"], "revoked": True}
    except tokens.TokenError as error:
        raise Problem(str(error)) from None
    raise Problem("Unknown operation route", 404)


# -- GET dispatch ----------------------------------------------------------------

def get(path: str, query: dict) -> dict:
    def one(key):
        values = query.get(key)
        if not values or len(values) != 1:
            raise Problem(f"'{key}' 값이 필요해요.")
        return values[0]
    if path == "/api/rag/state":
        return state()
    if path == "/api/rag/collections":
        return collections()
    if path == "/api/rag/documents":
        return documents_in(one("collection"))
    if path == "/api/rag/teams":
        _admin_only()
        from omm.cluster import tokens
        try:
            people = sorted(record.name for record in tokens.list_tokens())
        except tokens.TokenError:
            people = []
        return {"teams": rag_acl.teams(), "people": people}
    if path == "/api/rag/tokens":
        from omm.cluster import tokens
        if not tokens_enabled():
            raise Problem(TOKENS_ON_HEAD, 403)
        try:
            return {"tokens": [{"name": r.name, "created_at": r.created_at} for r in tokens.list_tokens()]}
        except tokens.TokenError as error:
            raise Problem(str(error), 500) from None
    raise Problem("Unknown API route", 404)
