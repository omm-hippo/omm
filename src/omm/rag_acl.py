"""Who may see and manage which document collection on a cluster head (#504).

Callers are either the head's own local user (the administrator, who can
already read the document store file) or an employee identified by the name
of the cluster token the gateway verified (``omm cluster token add <name>``).
A collection belongs to whoever created it; collections created before this
module existed, or by the local CLI, belong to the administrator. By default
only the owner and the administrator can search a collection. The owner or the
administrator can share it with people (token names) and teams; teams are kept
here, next to the documents, and only the administrator manages them.

The tables live in the document store's SQLite file. A collection without an
owner row is the administrator's, so an old store needs no data migration and
nothing becomes visible to employees until it is shared.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid

from omm import documents

ADMIN_OWNER = "@admin"  # '@' cannot appear in a cluster token name.
_NAME_RE = re.compile(r"^[\w.-]{1,40}$")
_TABLES = """
CREATE TABLE IF NOT EXISTS rag_owners (collection_id TEXT PRIMARY KEY, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rag_shares (
    collection_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('person','team')),
    target TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(collection_id, kind, target));
CREATE TABLE IF NOT EXISTS rag_teams (name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rag_team_members (team TEXT NOT NULL, member TEXT NOT NULL, PRIMARY KEY(team, member));
"""


class AccessError(ValueError):
    """A refused or impossible request. ``status`` maps to an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Caller:
    """``name`` is a cluster token name; None is the head's local administrator."""

    name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.name is None

    @property
    def owner_key(self) -> str:
        return ADMIN_OWNER if self.name is None else self.name


ADMIN = Caller()
NO_SHARED_DOCUMENTS = ("검색할 수 있는 문서가 없어요. 내가 만든 컬렉션에 문서를 넣거나(`omm rag add <컬렉션> <파일>`), "
                       "컬렉션 주인에게 공유(`omm rag share`)를 부탁해 주세요.")


def employee(name: str) -> Caller:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AccessError("직원 이름(토큰 이름)을 확인해 주세요.")
    return Caller(name)


def owner_label(owner: str) -> str:
    return "관리자" if owner == ADMIN_OWNER else owner


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _not_found(name) -> AccessError:
    # The same answer for a missing collection and one the caller cannot see.
    return AccessError(f"'{name}' 컬렉션을 찾지 못했어요. `omm rag collection list`로 이름을 확인해 주세요.", 404)


class _Access:
    """One open connection to the document store with the ACL tables."""

    def __init__(self, store=None):
        self.store = store or documents.DocumentStore()

    def connect(self):
        return self.store.connection()

    @staticmethod
    def tables(db):
        db.executescript(_TABLES)


def _rows(db) -> list[dict]:
    _Access.tables(db)
    return [dict(row) for row in db.execute(
        "SELECT c.id, c.name, c.created_at, COALESCE(o.owner, ?) AS owner, "
        "(SELECT COUNT(*) FROM documents WHERE collection_id=c.id) AS documents "
        "FROM collections c LEFT JOIN rag_owners o ON o.collection_id=c.id "
        "ORDER BY c.id != 'default', c.name", (ADMIN_OWNER,))]


def _teams_of(db, name: str) -> set[str]:
    return {row[0] for row in db.execute("SELECT team FROM rag_team_members WHERE member=?", (name,))}


def _visible(db, caller: Caller) -> list[dict]:
    rows = _rows(db)
    if caller.is_admin:
        return rows
    teams = _teams_of(db, caller.name)
    shared = set()
    for collection_id, kind, target in db.execute("SELECT collection_id, kind, target FROM rag_shares"):
        if (kind == "person" and target == caller.name) or (kind == "team" and target in teams):
            shared.add(collection_id)
    return [row for row in rows if row["owner"] == caller.name or row["id"] in shared]


def _can_manage(caller: Caller, row: dict) -> bool:
    return caller.is_admin or row["owner"] == caller.name


def _shares(db, collection_id) -> list[dict]:
    return [{"kind": row[0], "target": row[1]} for row in db.execute(
        "SELECT kind, target FROM rag_shares WHERE collection_id=? ORDER BY kind, target", (collection_id,))]


def _find(db, caller: Caller, name, *, manage=False) -> dict:
    row = next((row for row in _visible(db, caller) if row["name"] == name), None)
    if row is None:
        raise _not_found(name)
    if manage and not _can_manage(caller, row):
        raise AccessError(f"'{name}' 컬렉션은 만든 사람({owner_label(row['owner'])})이나 관리자만 바꿀 수 있어요.", 403)
    return row


# -- collections -----------------------------------------------------------

def collections(caller: Caller, *, store=None) -> list[dict]:
    """Collections the caller can search, with owner; shares only where the caller manages."""
    with _Access(store).connect() as db:
        rows = _visible(db, caller)
        for row in rows:
            row["can_manage"] = _can_manage(caller, row)
            row["shares"] = _shares(db, row["id"]) if row["can_manage"] else None
    return rows


def visible_ids(caller: Caller, *, store=None) -> set[str]:
    with _Access(store).connect() as db:
        return {row["id"] for row in _visible(db, caller)}


def create_collection(caller: Caller, name, *, store=None) -> dict:
    name = documents._check_collection_name(name)
    with _Access(store).connect() as db:
        _Access.tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM collections WHERE name=?", (name,)).fetchone():
            # Names are unique on the head; the message does not say whose it is.
            raise AccessError(f"'{name}' 이름은 쓸 수 없어요. 이미 있는 이름이에요. 다른 이름을 골라 주세요.", 409)
        key = str(uuid.uuid4())
        db.execute("INSERT INTO collections VALUES(?,?,?)", (key, name, _now()))
        db.execute("INSERT INTO rag_owners VALUES(?,?)", (key, caller.owner_key))
    return {"id": key, "name": name, "owner": caller.owner_key}


def delete_collection(caller: Caller, name, *, store=None) -> dict:
    access = _Access(store)
    with access.connect() as db:
        row = _find(db, caller, name, manage=True)
    result = access.store.delete_collection(name)
    with access.connect() as db:
        _Access.tables(db)
        db.execute("DELETE FROM rag_owners WHERE collection_id=?", (row["id"],))
        db.execute("DELETE FROM rag_shares WHERE collection_id=?", (row["id"],))
    return result


def documents_in(caller: Caller, name, *, store=None) -> list[dict]:
    access = _Access(store)
    with access.connect() as db:
        _find(db, caller, name)
    return access.store.list(name)


def import_document(caller: Caller, collection, filename, content: bytes, *, replace=False, store=None) -> dict:
    access = _Access(store)
    with access.connect() as db:
        _find(db, caller, collection, manage=True)
    if len(content) > documents.MAX_UPLOAD:
        raise AccessError("비어 있거나 6 MiB를 넘는 문서는 등록할 수 없어요.", 413)
    return access.store.import_document(filename, content, replace=replace, collection=collection)


def remove_document(caller: Caller, collection, name, *, store=None) -> dict:
    access = _Access(store)
    with access.connect() as db:
        _find(db, caller, collection, manage=True)
    return access.store.remove_document(collection, name)


# -- shares ----------------------------------------------------------------

def _known_people() -> set[str]:
    from omm.cluster import tokens
    try:
        return {record.name for record in tokens.list_tokens()}
    except tokens.TokenError as error:
        raise AccessError("직원 토큰 목록을 읽지 못했어요. 헤드의 `omm cluster token list`를 확인해 주세요.", 500) from error


def _target(db, person, team) -> tuple[str, str]:
    if (person is None) == (team is None):
        raise AccessError("공유할 직원 이름이나 --team 팀 이름 중 하나만 정해 주세요.")
    if person is not None:
        if not isinstance(person, str) or not _NAME_RE.match(person) or person not in _known_people():
            raise AccessError(f"'{person}' 직원 토큰이 없어요. 토큰 이름(`omm cluster token list`)으로 정해 주세요.", 404)
        return "person", person
    if not isinstance(team, str) or not db.execute("SELECT 1 FROM rag_teams WHERE name=?", (team,)).fetchone():
        raise AccessError(f"'{team}' 팀이 없어요. 관리자에게 팀 이름을 확인해 주세요.", 404)
    return "team", team


def shares(caller: Caller, collection, *, store=None) -> dict:
    with _Access(store).connect() as db:
        row = _find(db, caller, collection, manage=True)
        return {"collection": row["name"], "owner": row["owner"], "shares": _shares(db, row["id"])}


def share(caller: Caller, collection, *, person=None, team=None, store=None) -> dict:
    with _Access(store).connect() as db:
        row = _find(db, caller, collection, manage=True)
        kind, target = _target(db, person, team)
        if kind == "person" and target == row["owner"]:
            raise AccessError("만든 사람은 공유하지 않아도 이미 검색할 수 있어요.")
        db.execute("BEGIN IMMEDIATE")
        added = db.execute("INSERT OR IGNORE INTO rag_shares VALUES(?,?,?,?)",
                           (row["id"], kind, target, _now())).rowcount == 1
    return {"collection": row["name"], "kind": kind, "target": target, "added": added}


def unshare(caller: Caller, collection, *, person=None, team=None, store=None) -> dict:
    if (person is None) == (team is None):
        raise AccessError("공유를 거둘 직원 이름이나 --team 팀 이름 중 하나만 정해 주세요.")
    kind, target = ("person", person) if person is not None else ("team", team)
    with _Access(store).connect() as db:
        row = _find(db, caller, collection, manage=True)
        db.execute("BEGIN IMMEDIATE")
        removed = db.execute("DELETE FROM rag_shares WHERE collection_id=? AND kind=? AND target=?",
                             (row["id"], kind, target)).rowcount == 1
    return {"collection": row["name"], "kind": kind, "target": target, "removed": removed}


# -- teams (administrator only) ---------------------------------------------

def _admin_only(caller: Caller) -> None:
    if not caller.is_admin:
        raise AccessError("팀은 헤드 Mac의 관리자만 관리할 수 있어요.", 403)


def _team_name(name) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AccessError("팀 이름은 1~40자의 글자, 숫자, '.', '-', '_' 로 정해 주세요.")
    return name


def teams(caller: Caller = ADMIN, *, store=None) -> list[dict]:
    _admin_only(caller)
    with _Access(store).connect() as db:
        _Access.tables(db)
        result = []
        for row in db.execute("SELECT name, created_at FROM rag_teams ORDER BY name").fetchall():
            members = [r[0] for r in db.execute("SELECT member FROM rag_team_members WHERE team=? ORDER BY member", (row[0],))]
            result.append({"name": row[0], "created_at": row[1], "members": members})
    return result


def team_create(name, caller: Caller = ADMIN, *, store=None) -> dict:
    _admin_only(caller)
    name = _team_name(name)
    with _Access(store).connect() as db:
        _Access.tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("INSERT OR IGNORE INTO rag_teams VALUES(?,?)", (name, _now())).rowcount != 1:
            raise AccessError(f"'{name}' 팀이 이미 있어요.", 409)
    return {"name": name}


def team_delete(name, caller: Caller = ADMIN, *, store=None) -> dict:
    """Delete a team, its membership and every share made to it."""
    _admin_only(caller)
    with _Access(store).connect() as db:
        _Access.tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("DELETE FROM rag_teams WHERE name=?", (name,)).rowcount != 1:
            raise AccessError(f"'{name}' 팀이 없어요.", 404)
        db.execute("DELETE FROM rag_team_members WHERE team=?", (name,))
        shares_removed = db.execute("DELETE FROM rag_shares WHERE kind='team' AND target=?", (name,)).rowcount
    return {"name": name, "shares_removed": shares_removed}


def team_add(team, member, caller: Caller = ADMIN, *, store=None) -> dict:
    _admin_only(caller)
    if not isinstance(member, str) or not _NAME_RE.match(member) or member not in _known_people():
        raise AccessError(f"'{member}' 직원 토큰이 없어요. 토큰 이름(`omm cluster token list`)으로 정해 주세요.", 404)
    with _Access(store).connect() as db:
        _Access.tables(db)
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM rag_teams WHERE name=?", (team,)).fetchone():
            raise AccessError(f"'{team}' 팀이 없어요. `omm rag team create {team}`로 먼저 만들어 주세요.", 404)
        added = db.execute("INSERT OR IGNORE INTO rag_team_members VALUES(?,?)", (team, member)).rowcount == 1
    return {"team": team, "member": member, "added": added}


def team_remove(team, member, caller: Caller = ADMIN, *, store=None) -> dict:
    _admin_only(caller)
    with _Access(store).connect() as db:
        _Access.tables(db)
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM rag_teams WHERE name=?", (team,)).fetchone():
            raise AccessError(f"'{team}' 팀이 없어요.", 404)
        removed = db.execute("DELETE FROM rag_team_members WHERE team=? AND member=?", (team, member)).rowcount == 1
    return {"team": team, "member": member, "removed": removed}


# -- search scoped to one caller --------------------------------------------

class ScopedStore:
    """The document store as one employee sees it: only searchable collections.

    It offers what :mod:`omm.rag_adapter` uses (``list``, ``collections``,
    ``search``, ``search_detailed``). Visibility is read once, so one request
    sees one consistent set of collections.
    """

    def __init__(self, caller: Caller, store=None):
        self._store = store or documents.DocumentStore()
        self._caller = caller
        self._ids = visible_ids(caller, store=self._store)

    def _check(self, collection):
        if collection is not None and collection not in {row["name"] for row in self.collections()}:
            raise _not_found(collection)

    def collections(self):
        return [row for row in self._store.collections() if row["id"] in self._ids]

    def list(self, collection=None):
        self._check(collection)
        return [row for row in self._store.list(collection) if row["collection_id"] in self._ids]

    def search(self, question, ids, *, collection=None, limit=3):
        result = self.search_detailed(question, ids, collection=collection, limit=limit)
        return result["revision"], result["sources"]

    def search_detailed(self, question, ids=None, *, collection=None, limit=3):
        self._check(collection)
        allowed = {row["id"] for row in self.list()}
        if ids is None and collection is None:
            raise AccessError("검색할 컬렉션을 정해 주세요.")
        if ids is not None and not set(ids) <= allowed:
            raise AccessError("선택한 문서가 바뀌었어요. 문서 목록을 새로고침해 주세요.", 404)
        return self._store.search_detailed(question, ids, collection=collection, limit=limit)


def scoped_store(caller: Caller, store=None):
    if caller.is_admin:
        return store or documents.DocumentStore()
    return ScopedStore(caller, store)


class ScopedAdapter:
    """:mod:`omm.rag_adapter` functions bound to one caller's view, for the MCP server."""

    def __init__(self, caller: Caller, store=None):
        self._caller, self._base = caller, store

    def _view(self):
        return scoped_store(self._caller, self._base)

    def search(self, query, collection=None, limit=None):
        from omm import rag_adapter
        result = rag_adapter.search(query, collection, limit, store=self._view())
        if not self._caller.is_admin and result["message"] == rag_adapter.NO_DOCUMENTS:
            result["message"] = NO_SHARED_DOCUMENTS
        return result

    def list_collections(self):
        from omm import rag_adapter
        return rag_adapter.list_collections(store=self._view())
