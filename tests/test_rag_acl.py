"""Company document sharing on the cluster head (#504 phase 2).

Permissions in omm.rag_acl, the gateway's /rag and /mcp routes through a real
GatewayServer on 127.0.0.1, the employee-side `omm rag remote` CLI against it,
and `omm rag connect --remote` harness entries.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import sqlite3
import threading

import pytest
from typer.testing import CliRunner

from omm import cli, config, documents, rag_acl, rag_connect, rag_remote
from omm.cluster import gateway, tokens
from tests import _gateway_tls
from omm.documents import DocumentStore

HR = "연차 휴가는 입사 첫 해에 열하루까지 쓸 수 있습니다.\n연차 휴가 신청은 팀장 승인을 받아야 합니다.\n".encode()
SALES = "영업 보고서는 매주 금요일 오후에 영업팀 공유 폴더에 올립니다.\n".encode()


@pytest.fixture
def lexical(isolated_omm_home, monkeypatch):
    """Embeddings point at a closed loopback port: lexical search with a notice."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    DocumentStore().set_embedding_model("bge-m3", base_url=f"http://127.0.0.1:{port}")
    return isolated_omm_home


@pytest.fixture
def people(lexical):
    return {name: tokens.add_token(name) for name in ("kim", "lee", "park")}


def _search(caller, query="연차 휴가 신청", collection=None):
    return rag_acl.ScopedAdapter(caller).search(query, collection)


def _names(caller):
    return [row["name"] for row in rag_acl.collections(caller)]


# ------------------------------------------------------------ permissions --


def test_old_store_collections_become_admin_owned_and_unshared(lexical):
    store = DocumentStore()
    store.create_collection("인사규정")  # Created before the ACL tables existed.
    store.import_document("hr.txt", HR, collection="인사규정")
    db = sqlite3.connect(config.OMM_HOME / "documents" / "library.sqlite3")
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='rag_owners'").fetchone()
    db.close()
    tokens.add_token("kim")
    rows = {row["name"]: row for row in rag_acl.collections(rag_acl.ADMIN)}
    assert rows["인사규정"]["owner"] == rag_acl.ADMIN_OWNER and rows["인사규정"]["shares"] == []
    assert _names(rag_acl.employee("kim")) == []
    assert _search(rag_acl.employee("kim"))["found"] is False
    assert _search(rag_acl.ADMIN)["found"] is True


def test_owner_only_by_default_then_person_share_and_unshare(people):
    kim, lee = rag_acl.employee("kim"), rag_acl.employee("lee")
    rag_acl.create_collection(kim, "인사규정")
    rag_acl.import_document(kim, "인사규정", "hr.txt", HR)
    assert _names(kim) == ["인사규정"] and _names(lee) == []
    assert _search(kim, collection="인사규정")["found"] is True
    assert _search(lee)["found"] is False and _search(lee)["message"] == rag_acl.no_shared_documents()
    with pytest.raises(ValueError, match="No document collection"):
        _search(lee, collection="인사규정")  # Same answer as a name that does not exist.
    assert rag_acl.share(kim, "인사규정", person="lee")["added"] is True
    assert _search(lee, collection="인사규정")["found"] is True
    assert rag_acl.collections(lee)[0]["shares"] is None  # Only the owner sees the share list.
    assert rag_acl.unshare(kim, "인사규정", person="lee")["removed"] is True
    assert _search(lee)["found"] is False and _names(lee) == []


def test_team_share_follows_membership_and_team_delete(people):
    kim, lee, park = (rag_acl.employee(n) for n in ("kim", "lee", "park"))
    rag_acl.create_collection(kim, "영업자료")
    rag_acl.import_document(kim, "영업자료", "sales.txt", SALES)
    rag_acl.team_create("sales")
    rag_acl.team_add("sales", "lee")
    rag_acl.share(kim, "영업자료", team="sales")
    query = "영업 보고서 금요일"
    assert _search(lee, query)["found"] is True
    assert _search(park, query)["found"] is False
    rag_acl.team_add("sales", "park")
    assert _search(park, query)["found"] is True
    rag_acl.team_remove("sales", "park")
    assert _search(park, query)["found"] is False
    assert rag_acl.team_delete("sales")["shares_removed"] == 1
    assert _search(lee, query)["found"] is False


def test_management_is_owner_or_admin_only(people):
    kim, lee = rag_acl.employee("kim"), rag_acl.employee("lee")
    rag_acl.create_collection(kim, "인사규정")
    rag_acl.share(kim, "인사규정", person="lee")
    for action in (lambda: rag_acl.import_document(lee, "인사규정", "x.txt", HR),
                   lambda: rag_acl.share(lee, "인사규정", person="park"),
                   lambda: rag_acl.unshare(lee, "인사규정", person="lee"),
                   lambda: rag_acl.delete_collection(lee, "인사규정"),
                   lambda: rag_acl.remove_document(lee, "인사규정", "x.txt")):
        with pytest.raises(rag_acl.AccessError) as caught:
            action()
        assert caught.value.status == 403
    with pytest.raises(rag_acl.AccessError) as caught:
        rag_acl.shares(rag_acl.employee("park"), "인사규정")
    assert caught.value.status == 404  # Invisible looks exactly like missing.
    with pytest.raises(rag_acl.AccessError) as caught:
        rag_acl.team_create("x", lee)
    assert caught.value.status == 403
    # The administrator manages everyone's collections.
    rag_acl.import_document(rag_acl.ADMIN, "인사규정", "hr.txt", HR)
    rag_acl.unshare(rag_acl.ADMIN, "인사규정", person="lee")
    assert rag_acl.delete_collection(rag_acl.ADMIN, "인사규정")["documents"] == 1


def test_share_targets_must_exist_and_upload_limit(people):
    kim = rag_acl.employee("kim")
    rag_acl.create_collection(kim, "c")
    with pytest.raises(rag_acl.AccessError, match="직원 토큰이 없어요"):
        rag_acl.share(kim, "c", person="nobody")
    with pytest.raises(rag_acl.AccessError, match="팀이 없어요"):
        rag_acl.share(kim, "c", team="ghost")
    with pytest.raises(rag_acl.AccessError):
        rag_acl.share(kim, "c", person="lee", team="ghost")
    with pytest.raises(rag_acl.AccessError) as caught:
        rag_acl.import_document(kim, "c", "big.txt", b"a" * (documents.MAX_UPLOAD + 1))
    assert caught.value.status == 413
    with pytest.raises(rag_acl.AccessError) as caught:
        rag_acl.create_collection(rag_acl.employee("lee"), "c")
    assert caught.value.status == 409


# --------------------------------------------------------- gateway routes --


@pytest.fixture
def head(people):
    server = gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server, people
    server.shutdown()
    server.server_close()


def _call(server, method, path, token=None, body=None, headers=None):
    conn = _gateway_tls.connection(server, timeout=30)
    sent = dict(headers or {})
    if token:
        sent["Authorization"] = f"Bearer {token}"
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), (json.loads(payload) if payload else None)


def _rpc(server, token, message, version="2025-06-18"):
    headers = {"Accept": "application/json, text/event-stream"}
    if version:
        headers["MCP-Protocol-Version"] = version
    return _call(server, "POST", "/mcp", token, message, headers)


def test_rag_routes_need_a_valid_token(head):
    server, _ = head
    for token in (None, "omm_wrong"):
        status, _, body = _call(server, "GET", "/rag/v1/collections", token)
        assert status == 401 and body["error"]["type"] == "authentication_error"
        assert _rpc(server, token, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401


def test_rest_permission_matrix_and_revoke(head):
    server, t = head
    assert _call(server, "GET", "/rag/v1/whoami", t["kim"])[2] == {"name": "kim"}
    status, _, body = _call(server, "POST", "/rag/v1/collections", t["kim"], {"name": "인사규정"})
    assert status == 201 and body["owner"] == "kim"
    from urllib.parse import quote
    status, _, body = _call(server, "POST", "/rag/v1/documents?collection=" + quote("인사규정"), t["kim"], HR,
                            {"X-OMM-Filename": quote("휴가 규정.txt")})
    assert status == 201 and body["name"] == "휴가 규정.txt" and body["semantic"] is False
    search = {"query": "연차 휴가 신청", "collection": "인사규정"}
    assert _call(server, "POST", "/rag/v1/search", t["kim"], search)[2]["found"] is True
    status, _, body = _call(server, "POST", "/rag/v1/search", t["lee"], search)
    assert status == 400 and "No document collection" in body["error"]["message"]
    assert _call(server, "GET", "/rag/v1/collections", t["lee"])[2] == {"collections": []}
    # Non-owner management: invisible is 404, visible-but-not-owner is 403.
    shares_path = "/rag/v1/shares?collection=" + quote("인사규정")
    assert _call(server, "GET", shares_path, t["lee"])[0] == 404
    assert _call(server, "POST", "/rag/v1/shares", t["kim"], {"collection": "인사규정", "person": "lee"})[0] == 200
    assert _call(server, "POST", "/rag/v1/search", t["lee"], search)[2]["found"] is True
    assert _call(server, "GET", shares_path, t["lee"])[0] == 403
    status, _, body = _call(server, "POST", "/rag/v1/documents?collection=" + quote("인사규정"), t["lee"], HR,
                            {"X-OMM-Filename": "x.txt"})
    assert status == 403 and body["error"]["type"] == "permission_error"
    assert _call(server, "GET", shares_path, t["kim"])[2]["shares"] == [{"kind": "person", "target": "lee"}]
    # Revoking lee's token closes the door even though the share remains.
    tokens.revoke_token("lee")
    assert _call(server, "POST", "/rag/v1/search", t["lee"], search)[0] == 401
    status, _, _ = _call(server, "DELETE", shares_path + "&person=lee", t["kim"])
    assert status == 200
    assert _call(server, "GET", "/rag/v1/nothing", t["kim"])[0] == 404


def test_rest_messages_follow_accept_language(head):
    server, t = head
    path = "/rag/v1/documents?collection=missing"
    korean = _call(server, "GET", path, t["kim"])[2]["error"]["message"]
    english = _call(server, "GET", path, t["kim"], headers={"Accept-Language": "en"})[2]["error"]["message"]
    assert "찾지 못했어요" in korean  # Older employee PCs send no header and keep Korean.
    assert english == "Collection 'missing' was not found. Check the name with `omm rag collection list`."


def test_upload_size_limits(head, monkeypatch):
    server, t = head
    _call(server, "POST", "/rag/v1/collections", t["kim"], {"name": "c"})
    status, _, body = _call(server, "POST", "/rag/v1/documents?collection=c", t["kim"],
                            b"a" * (documents.MAX_UPLOAD + 1), {"X-OMM-Filename": "big.txt"})
    assert status == 413 and "6 MiB" in body["error"]["message"]
    status, _, _ = _call(server, "POST", "/rag/v1/collections", t["kim"], b"{" + b" " * 70_000 + b"}")
    assert status == 413
    monkeypatch.setattr(gateway, "MAX_BODY_BYTES", 10)
    status, _, _ = _call(server, "POST", "/rag/v1/collections", t["kim"], {"name": "too long for the gateway"})
    assert status == 413


def test_mcp_over_http_round_trip_scoped_per_token(head):
    server, t = head
    kim = rag_acl.employee("kim")
    rag_acl.create_collection(kim, "인사규정")
    rag_acl.import_document(kim, "인사규정", "hr.txt", HR)
    status, headers, body = _rpc(server, t["kim"], {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, version=None)
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert "cluster head" in body["result"]["instructions"]
    status, _, body = _rpc(server, t["kim"], {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None
    names = [tool["name"] for tool in _rpc(server, t["kim"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[2]["result"]["tools"]]
    assert names == ["search_documents", "list_collections"]
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_documents", "arguments": {"query": "연차 휴가 신청"}}}
    result = _rpc(server, t["kim"], call)[2]["result"]
    assert result["structuredContent"]["found"] is True
    assert result["structuredContent"]["results"][0]["quote"] in HR.decode()
    other = _rpc(server, t["lee"], call)[2]["result"]
    assert other["structuredContent"]["found"] is False
    listed = _rpc(server, t["lee"], {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "list_collections", "arguments": {}}})[2]["result"]
    assert "인사규정" not in json.dumps(listed, ensure_ascii=False)
    rag_acl.share(kim, "인사규정", person="lee")
    assert _rpc(server, t["lee"], call)[2]["result"]["structuredContent"]["found"] is True
    rag_acl.unshare(kim, "인사규정", person="lee")
    assert _rpc(server, t["lee"], call)[2]["result"]["structuredContent"]["found"] is False


def test_mcp_transport_rules(head):
    server, t = head
    status, headers, _ = _call(server, "GET", "/mcp", t["kim"], headers={"Accept": "text/event-stream"})
    assert status == 405 and headers["Allow"] == "POST"
    assert _call(server, "DELETE", "/mcp", t["kim"])[0] == 405
    assert _rpc(server, t["kim"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}, version="1999-01-01")[0] == 400
    status, _, _ = _call(server, "POST", "/mcp", t["kim"], {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         {"Origin": "http://evil.example"})
    assert status == 403
    assert _rpc(server, t["kim"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})[2]["result"] == {}


def test_gateway_log_never_records_names_or_questions(head, caplog):
    server, t = head
    caplog.set_level("INFO", logger="omm.cluster.gateway")
    _call(server, "POST", "/rag/v1/collections", t["kim"], {"name": "비밀프로젝트"})
    _call(server, "POST", "/rag/v1/search", t["kim"], {"query": "비밀질문", "collection": "비밀프로젝트"})
    _call(server, "GET", "/rag/v1/shares?collection=%EB%B9%84%EB%B0%80%ED%94%84%EB%A1%9C%EC%A0%9D%ED%8A%B8", t["kim"])
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "/rag/v1/search" in text
    assert "비밀" not in text and "%EB%B9%84" not in text


# ------------------------------------------------- employee CLI (remote) --


def _cli(*args, input=None):
    result = CliRunner().invoke(cli.app, list(args), input=input)
    return result


@pytest.fixture
def employee_pc(head, tmp_path, monkeypatch):
    """Head and employee share this test process; the employee's OMM_HOME only holds rag-remote.json."""
    server, t = head
    url = _gateway_tls.url(server)
    # The employee's remote file lives in a separate folder; the head's store stays where it is.
    pc_home = tmp_path / "employee-pc"
    pc_home.mkdir()
    monkeypatch.setattr(rag_remote, "remote_path", lambda: pc_home / "rag-remote.json")
    return url, t, pc_home


def test_remote_cli_end_to_end(employee_pc, tmp_path):
    url, t, pc_home = employee_pc
    bad = _cli("rag", "remote", "set", url, "--yes", input="omm_wrong\n")
    assert bad.exit_code == 1 and "rejected the token" in bad.output
    assert not (pc_home / "rag-remote.json").exists()
    assert not rag_remote.ca_path().exists()  # nothing is pinned for a refused token
    ok = _cli("rag", "remote", "set", url, "--yes", input=t["kim"] + "\n")
    assert ok.exit_code == 0, ok.output
    assert "'kim'" in ok.output and t["kim"] not in ok.output
    assert t["kim"] not in _cli("rag", "remote").output
    assert _cli("rag", "collection", "create", "인사규정").exit_code == 0
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "hr.txt").write_bytes(HR)
    (folder / "skip.bin").write_bytes(b"x")
    added = _cli("rag", "add", "인사규정", str(folder))
    assert added.exit_code == 0, added.output
    assert "Added: hr.txt" in added.output and "Skipped 1 file" in added.output
    found = _cli("rag", "search", "연차 휴가 신청", "--collection", "인사규정")
    assert found.exit_code == 0 and "hr.txt" in found.output and "연차 휴가 신청은" in found.output
    listed = _cli("rag", "collection", "list")
    assert "created by kim" in listed.output and "not shared" in listed.output
    assert _cli("rag", "share", "인사규정", "lee").exit_code == 0
    assert "shared with: lee" in _cli("rag", "collection", "list").output
    # lee sees it now, from their own PC.
    assert _cli("rag", "remote", "set", url, "--token", t["lee"]).exit_code == 0
    assert "hr.txt" in _cli("rag", "search", "연차 휴가 신청").output
    denied = _cli("rag", "share", "인사규정", "park")
    assert denied.exit_code == 1 and "Only the creator of '인사규정' (kim)" in " ".join(denied.output.split())
    assert _cli("rag", "team", "list").exit_code == 1  # Teams are managed on the head.
    _cli("rag", "remote", "set", url, "--token", t["kim"])
    assert _cli("rag", "unshare", "인사규정", "lee").exit_code == 0
    _cli("rag", "remote", "set", url, "--token", t["lee"])
    assert "hr.txt" not in _cli("rag", "search", "연차 휴가 신청").output
    _cli("rag", "remote", "set", url, "--token", t["kim"])
    assert _cli("rag", "remove", "인사규정", "hr.txt", "--yes").exit_code == 0
    assert _cli("rag", "remote", "clear").exit_code == 0
    assert not (pc_home / "rag-remote.json").exists()
    assert not rag_remote.ca_path().exists()


def test_head_admin_cli_share_team_and_list(people):
    assert _cli("rag", "collection", "create", "규정").exit_code == 0
    assert _cli("rag", "team", "create", "hr").exit_code == 0
    assert _cli("rag", "team", "add", "hr", "kim").exit_code == 0
    assert "hr: kim" in _cli("rag", "team", "list").output
    assert _cli("rag", "share", "규정", "--team", "hr").exit_code == 0
    assert _cli("rag", "share", "규정", "lee").exit_code == 0
    listed = _cli("rag", "collection", "list").output
    assert "created by administrator" in listed and "shared with: lee, team hr" in listed
    assert _cli("rag", "share", "규정", "nobody").exit_code == 1
    assert _cli("rag", "unshare", "규정", "--team", "hr").exit_code == 0
    assert _cli("rag", "team", "delete", "hr").exit_code == 0
    assert _cli("rag", "collection", "delete", "규정", "--yes").exit_code == 0


def test_remote_url_normalization():
    assert rag_remote.normalize_url("192.168.0.10") == "https://192.168.0.10:52440"
    assert rag_remote.normalize_url("https://head.local:8000/") == "https://head.local:8000"
    for bad in ("ftp://x", "https://h/mcp", "https://u:p@h"):
        with pytest.raises(rag_remote.RemoteError):
            rag_remote.normalize_url(bad)
    with pytest.raises(rag_remote.RemoteError, match="HTTPS만"):
        rag_remote.normalize_url("http://head.local:52440")


# --------------------------------------------- harness --remote entries --


@pytest.fixture
def harness_home(tmp_path, monkeypatch, isolated_omm_home):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rag_connect.shutil, "which", lambda name: "/installed/" + name)
    rag_remote.save(rag_remote.Remote("http://10.0.0.5:52440", "omm_secret"))
    return root


EXPECTED = {
    "claude-code": {"type": "http", "url": "http://10.0.0.5:52440/mcp", "headers": {"Authorization": "Bearer omm_secret"}},
    "cursor": {"url": "http://10.0.0.5:52440/mcp", "headers": {"Authorization": "Bearer omm_secret"}},
    "codex": {"url": "http://10.0.0.5:52440/mcp", "http_headers": {"Authorization": "Bearer omm_secret"}, "startup_timeout_sec": 30},
}


@pytest.mark.parametrize("harness", rag_connect.HARNESSES)
def test_connect_remote_writes_http_entry_and_restores(harness, harness_home):
    path = rag_connect.config_path(harness)
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = b'# mine\nmodel = "x"\n' if harness == "codex" else b'{"mcpServers": {"other": {"command": "x"}}}\n'
    path.write_bytes(seed)
    assert rag_connect.connect(harness, remote=True)["state"] == "added"
    raw = path.read_bytes()
    if harness == "codex":
        import tomllib
        entry = tomllib.loads(raw.decode())["mcp_servers"][rag_connect.SERVER_NAME]
    else:
        entry = json.loads(raw)["mcpServers"][rag_connect.SERVER_NAME]
    if harness == "pi":
        assert entry["type"] == "http" and entry["url"].endswith("/mcp") and entry["headers"] == EXPECTED["cursor"]["headers"]
    else:
        assert entry == EXPECTED[harness]
    assert rag_connect.status(harness)["remote"] is True
    assert rag_connect.connect(harness, remote=True)["state"] == "unchanged"
    # Switching back to this PC's server replaces the entry; disconnect restores the original bytes.
    assert rag_connect.connect(harness)["state"] == "updated"
    assert rag_connect.disconnect(harness)["state"] == "restored"
    assert path.read_bytes() == seed


def test_connect_remote_needs_a_remote(harness_home):
    rag_remote.clear()
    with pytest.raises(rag_connect.RagConnectionError, match="remote set"):
        rag_connect.connect("cursor", remote=True)
    assert not rag_connect.config_path("cursor").exists()


def test_cli_connect_remote_mentions_token_file(harness_home):
    result = _cli("rag", "connect", "cursor", "--remote")
    assert result.exit_code == 0, result.output
    assert "stored in that configuration file" in " ".join(result.output.split()) and "omm_secret" not in result.output
