"""내 문서 tab sharing API: local administrator, employee via a real head, tokens, agents.

The web server and a real cluster GatewayServer run in this process on
127.0.0.1. The head keeps the shared document store in the isolated
OMM_HOME; the employee PC's remote file lives in a separate folder.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import socket
import threading

import pytest
import requests

from omm import config, rag_acl, rag_connect, rag_remote
from omm.cluster import gateway, tokens
from omm.documents import DocumentStore
from omm.web import rag_sharing, worker
from omm.web.server import WebServer

HR = "연차 휴가는 입사 첫 해에 열하루까지 쓸 수 있습니다.\n연차 휴가 신청은 팀장 승인을 받아야 합니다.\n".encode()


@pytest.fixture
def lexical(isolated_omm_home):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    DocumentStore().set_embedding_model("bge-m3", base_url=f"http://127.0.0.1:{port}")
    return isolated_omm_home


@pytest.fixture
def agents_home(tmp_path, monkeypatch):
    """Harness configs live in a scratch HOME; real ~/.claude etc. are never touched."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rag_connect.shutil, "which", lambda name: "/installed/" + name)
    return root


@pytest.fixture
def web(tmp_path, lexical, agents_home):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>OMM</title>", encoding="utf-8")
    instance = WebServer(0, static_root=assets)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(5)


class Browser:
    def __init__(self, server):
        self.server = server
        self.client = requests.Session()
        assert self.client.get(server.url, timeout=5).status_code == 200
        self.seen = []

    def get(self, path):
        response = self.client.get(self.server.url + path.lstrip("/"), timeout=30)
        self.seen.append(response.text)
        return response.status_code, response.json()

    def post(self, path, body):
        response = self.client.post(self.server.url + path.lstrip("/"), json=body, headers={"X-OMM-Web": "1"}, timeout=30)
        self.seen.append(response.text)
        return response.status_code, response.json()


@pytest.fixture
def browser(web):
    return Browser(web)


def _names(browser):
    status, body = browser.get("/api/rag/collections")
    assert status == 200
    return {row["name"]: row for row in body["collections"]}


# ------------------------------------------------------------- boundaries --


def test_routes_need_session_and_same_origin_header(web):
    assert requests.get(web.url + "api/rag/state", timeout=5).status_code == 401
    assert requests.post(web.url + "api/rag/collections/create", json={"name": "x"}, timeout=5).status_code == 401
    browser = Browser(web)
    response = browser.client.post(web.url + "api/rag/collections/create", json={"name": "x"}, timeout=5)
    assert response.status_code == 403  # No X-OMM-Web header.
    response = browser.client.post(web.url + "api/rag/collections/create", json={"name": "x"},
                                   headers={"X-OMM-Web": "1", "Origin": "http://evil.example"}, timeout=5)
    assert response.status_code == 403
    assert "x" not in _names(browser)


def test_destructive_routes_require_confirmation(browser):
    assert browser.post("/api/rag/collections/create", {"name": "c"})[0] == 200
    for path, body in (("/api/rag/collections/delete", {"name": "c"}),
                       ("/api/rag/documents/remove", {"collection": "c", "name": "x"}),
                       ("/api/rag/unshare", {"collection": "c", "kind": "team", "target": "t"}),
                       ("/api/rag/remote/clear", {}),
                       ("/api/rag/disconnect", {"harness": "cursor"})):
        status, value = browser.post(path, body)
        assert status == 400, path
        status, value = browser.post(path, {**body, "confirmed": False})
        assert status == 400, path
    assert "c" in _names(browser)


# ---------------------------------------------------- local administrator --


def test_local_admin_collections_documents_shares_and_teams(browser, web):
    status, state = browser.get("/api/rag/state")
    assert status == 200 and state["mode"] == "local" and state["admin"] is True and state["tokens"] is False
    assert {row["harness"] for row in state["harnesses"]} == set(rag_connect.HARNESSES)
    assert browser.post("/api/rag/collections/create", {"name": "인사규정"}) == (200, {"name": "인사규정", "shared": False})
    status, body = browser.post("/api/rag/collections/create", {"name": "인사규정"})
    assert status == 409 and "이미 있는 이름" in body["error"]
    row = _names(browser)["인사규정"]
    assert row["owner"] == "관리자" and row["role"] == "관리자" and row["can_manage"] and row["shares"] == []
    assert _names(browser)["기본"]["default"] is True

    # Upload goes through a worker job (real subprocess) with the collection.
    request_id = "6f0c3c1e-8d1f-4d7a-9a55-0a1b2c3d4e5f"
    status, job = browser.post("/api/rag/upload", {"collection": "인사규정", "name": "hr.txt", "replace": False,
                                                    "content": base64.b64encode(HR).decode(), "confirmed": True,
                                                    "request_id": request_id})
    assert status == 202 and job["operation"] == "document_import"
    record = _wait(web, request_id)
    assert record["status"] == "completed", record
    assert record["result"]["collection"] == "인사규정"
    status, docs = browser.get("/api/rag/documents?collection=" + requests.utils.quote("인사규정"))
    assert status == 200 and [d["name"] for d in docs["documents"]] == ["hr.txt"] and docs["documents"][0]["id"]
    status, found = browser.post("/api/rag/search", {"collection": "인사규정", "query": "연차 휴가 신청"})
    assert status == 200 and found["found"] is True and found["source"] == "local"
    assert "팀장 승인" in found["results"][0]["quote"]

    # Shares need a known token name or an existing team, exactly as the CLI.
    status, body = browser.post("/api/rag/share", {"collection": "인사규정", "kind": "person", "target": "kim"})
    assert status == 404 and "직원 토큰이 없어요" in body["error"]
    tokens.add_token("kim")
    assert browser.post("/api/rag/share", {"collection": "인사규정", "kind": "person", "target": "kim"})[1]["added"] is True
    status, body = browser.post("/api/rag/share", {"collection": "인사규정", "kind": "team", "target": "sales"})
    assert status == 404 and "팀이 없어요" in body["error"]
    assert browser.post("/api/rag/teams/create", {"name": "sales"}) == (200, {"name": "sales"})
    status, body = browser.post("/api/rag/teams/add", {"team": "sales", "member": "nobody"})
    assert status == 404 and "직원 토큰이 없어요" in body["error"]
    assert browser.post("/api/rag/teams/add", {"team": "sales", "member": "kim"})[1]["added"] is True
    assert browser.get("/api/rag/teams")[1] == {"teams": [{"name": "sales", "created_at": rag_acl.teams()[0]["created_at"], "members": ["kim"]}],
                                                "people": ["kim"]}
    assert browser.post("/api/rag/share", {"collection": "인사규정", "kind": "team", "target": "sales"})[1]["added"] is True
    assert _names(browser)["인사규정"]["shares"] == [{"kind": "person", "target": "kim"}, {"kind": "team", "target": "sales"}]
    assert rag_acl.visible_ids(rag_acl.employee("kim"))
    removed = browser.post("/api/rag/unshare", {"collection": "인사규정", "kind": "person", "target": "kim", "confirmed": True})
    assert removed == (200, {"collection": "인사규정", "kind": "person", "target": "kim", "removed": True})
    assert browser.post("/api/rag/teams/remove", {"team": "sales", "member": "kim", "confirmed": True})[1]["removed"] is True
    assert browser.post("/api/rag/teams/delete", {"name": "sales", "confirmed": True})[1]["shares_removed"] == 1
    assert _names(browser)["인사규정"]["shares"] == []

    assert browser.post("/api/rag/documents/remove", {"collection": "인사규정", "name": "hr.txt", "confirmed": True})[0] == 200
    assert browser.post("/api/rag/collections/delete", {"name": "인사규정", "confirmed": True}) == (200, {"name": "인사규정", "documents": 0})
    status, body = browser.post("/api/rag/collections/delete", {"name": "기본", "confirmed": True})
    assert status == 400 and "기본 컬렉션은 지울 수 없어요" in body["error"]


def test_upload_to_unknown_collection_is_refused_before_a_job(browser, web):
    status, body = browser.post("/api/rag/upload", {"collection": "없음", "name": "a.txt", "content": "YQ==",
                                                     "confirmed": True, "request_id": "6f0c3c1e-8d1f-4d7a-9a55-0a1b2c3d4e50"})
    assert status == 404 and "컬렉션을 찾지 못했어요" in body["error"]
    assert web.jobs.list() == []


def _wait(web, request_id, seconds=60):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        record = next((job for job in web.jobs.list() if job["id"] == request_id), None)
        if record and record["status"] in {"completed", "failed", "interrupted", "cancelled"}:
            return record
        time.sleep(0.2)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------ token panel --


def test_token_panel_only_on_a_head_and_token_shown_once(browser):
    assert browser.get("/api/rag/state")[1]["tokens"] is False
    status, body = browser.get("/api/rag/tokens")
    assert status == 403
    assert browser.post("/api/rag/tokens/add", {"name": "kim"})[0] == 403
    # A cluster head (cluster.json role head) gets the panel.
    path = config.OMM_HOME / "cluster" / "cluster.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"role": "head", "namespace": "omm-" + "a" * 32, "gateway_port": 52440}), encoding="utf-8")
    assert browser.get("/api/rag/state")[1]["tokens"] is True
    status, issued = browser.post("/api/rag/tokens/add", {"name": "kim"})
    assert status == 200 and issued["token"].startswith("omm_") and tokens.verify(issued["token"]) == "kim"
    secret = issued["token"]
    status, body = browser.post("/api/rag/tokens/add", {"name": "kim"})
    assert status == 400 and "already exists" in body["error"]
    status, listed = browser.get("/api/rag/tokens")
    assert listed == {"tokens": [{"name": "kim", "created_at": issued["created_at"]}]}
    assert secret not in json.dumps(listed)
    assert browser.post("/api/rag/tokens/revoke", {"name": "kim"})[0] == 400
    assert browser.post("/api/rag/tokens/revoke", {"name": "kim", "confirmed": True}) == (200, {"name": "kim", "revoked": True})
    assert tokens.verify(secret) is None
    assert browser.post("/api/rag/tokens/revoke", {"name": "kim", "confirmed": True})[0] == 404
    # Only the one issue response ever carried the token.
    assert sum(secret in text for text in browser.seen) == 1


def test_tokens_file_alone_enables_the_panel(browser):
    tokens.add_token("lee")
    assert browser.get("/api/rag/state")[1]["tokens"] is True
    assert browser.get("/api/rag/tokens")[1]["tokens"][0]["name"] == "lee"


# --------------------------------------------- employee against a real head --


@pytest.fixture
def head(lexical):
    people = {name: tokens.add_token(name) for name in ("kim", "lee")}
    server = gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", people
    server.shutdown()
    server.server_close()


@pytest.fixture
def employee(head, tmp_path, monkeypatch):
    url, people = head
    pc_home = tmp_path / "employee-pc"
    pc_home.mkdir()
    monkeypatch.setattr(rag_remote, "remote_path", lambda: pc_home / "rag-remote.json")
    return url, people, pc_home


def _upload_in_process(request, monkeypatch):
    """Run the worker's upload step here (the patched remote path is per process)."""
    events = []
    monkeypatch.setattr(worker, "emit", lambda event, **fields: events.append((event, fields)))
    return worker.execute(request, threading.Event()), events


def test_remote_mode_through_real_gateway(employee, web, monkeypatch):
    url, people, pc_home = employee
    kim = Browser(web)
    status, body = kim.post("/api/rag/remote/set", {"url": url, "token": "omm_wrong"})
    assert status == 400 and "토큰" in body["error"]
    assert not (pc_home / "rag-remote.json").exists()
    assert kim.post("/api/rag/remote/set", {"url": url, "token": " " + people["kim"] + " "}) == (200, {"url": url, "name": "kim"})
    status, state = kim.get("/api/rag/state")
    assert state["mode"] == "remote" and state["remote"] == {"url": url, "name": "kim"}
    assert state["admin"] is False and state["tokens"] is False

    assert kim.post("/api/rag/collections/create", {"name": "영업"})[0] == 200
    row = _names(kim)["영업"]
    assert row["owner"] == "kim" and row["role"] == "주인" and row["can_manage"] and row["shares"] == []

    # Upload job: the request names the head address but never the token.
    captured = {}

    def fake_start(request, request_id):
        captured["request"] = request
        return {"id": request_id, **{k: v for k, v in request.items() if k != "content"}}
    monkeypatch.setattr(web.jobs, "start", fake_start)
    status, job = kim.post("/api/rag/upload", {"collection": "영업", "name": "hr.txt", "replace": False,
                                                "content": base64.b64encode(HR).decode(), "confirmed": True,
                                                "request_id": "6f0c3c1e-8d1f-4d7a-9a55-0a1b2c3d4e51"})
    assert status == 202 and job["remote_url"] == url and job["collection"] == "영업"
    request = captured["request"]
    assert set(request) == {"operation", "filename", "content", "replace", "collection", "remote_url"}
    assert people["kim"] not in json.dumps(request)
    result, events = _upload_in_process(request, monkeypatch)
    assert result["name"] == "hr.txt" and result["collection"] == "영업" and result["chunks"] >= 1
    assert events == [("progress", {"document_stage": "upload", "completed_count": 0, "total_count": 0})]
    assert people["kim"] not in json.dumps(result)
    # A different head than the one the request was made for is refused.
    with pytest.raises(ValueError, match="연결한 곳이 바뀌었어요"):
        _upload_in_process({**request, "remote_url": "http://127.0.0.1:9"}, monkeypatch)

    status, docs = kim.get("/api/rag/documents?collection=" + requests.utils.quote("영업"))
    assert [d["name"] for d in docs["documents"]] == ["hr.txt"] and "id" not in docs["documents"][0]
    status, found = kim.post("/api/rag/search", {"collection": "영업", "query": "연차 휴가 신청"})
    assert status == 200 and found["found"] is True and found["source"] == "remote" and "팀장 승인" in found["results"][0]["quote"]

    # Teams and tokens are the head administrator's, not the employee's.
    status, body = kim.get("/api/rag/teams")
    assert status == 403 and body["error"] == rag_sharing.TEAMS_ON_HEAD
    assert kim.post("/api/rag/teams/create", {"name": "t"})[0] == 403
    assert kim.get("/api/rag/tokens")[0] == 403

    assert kim.post("/api/rag/share", {"collection": "영업", "kind": "person", "target": "lee"})[1]["added"] is True
    assert _names(kim)["영업"]["shares"] == [{"kind": "person", "target": "lee"}]

    # Switch the same browser page to lee: the shared collection is visible, read-only.
    assert kim.post("/api/rag/remote/set", {"url": url, "token": people["lee"]})[1]["name"] == "lee"
    lee = kim
    row = _names(lee)["영업"]
    assert row["role"] == "공유받음" and row["can_manage"] is False and row["shares"] is None and row["owner"] == "kim"
    assert lee.post("/api/rag/search", {"collection": "영업", "query": "연차 휴가 신청"})[1]["found"] is True
    status, body = lee.post("/api/rag/documents/remove", {"collection": "영업", "name": "hr.txt", "confirmed": True})
    assert status == 400 and "만든 사람(kim)이나 관리자만" in body["error"]
    status, body = lee.post("/api/rag/unshare", {"collection": "영업", "kind": "person", "target": "lee", "confirmed": True})
    assert status == 400 and "관리자만" in body["error"]

    # Back to kim, unshare; lee no longer sees it.
    kim.post("/api/rag/remote/set", {"url": url, "token": people["kim"]})
    assert kim.post("/api/rag/unshare", {"collection": "영업", "kind": "person", "target": "lee", "confirmed": True})[1]["removed"] is True
    kim.post("/api/rag/remote/set", {"url": url, "token": people["lee"]})
    assert "영업" not in _names(lee)
    kim.post("/api/rag/remote/set", {"url": url, "token": people["kim"]})
    assert kim.post("/api/rag/collections/delete", {"name": "영업", "confirmed": True})[1]["documents"] == 1

    assert kim.post("/api/rag/remote/clear", {"confirmed": True}) == (200, {"removed": True})
    assert kim.get("/api/rag/state")[1]["mode"] == "local"
    # No response the browser ever saw carried an employee token.
    for secret in people.values():
        assert not any(secret in text for text in kim.seen)
    assert not any(secret in json.dumps(job) for job in web.jobs.list() for secret in people.values())


def test_broken_remote_file_is_reported_and_clearable(employee, browser):
    _, _, pc_home = employee
    (pc_home / "rag-remote.json").write_text("{", encoding="utf-8")
    status, state = browser.get("/api/rag/state")
    assert status == 200 and state["mode"] == "remote" and "clear" in state["error"]
    assert browser.post("/api/rag/remote/clear", {"confirmed": True})[1] == {"removed": True}


def test_unreachable_head_keeps_the_token_out_of_the_error(employee, browser):
    rag_remote.save(rag_remote.Remote("http://127.0.0.1:9", "omm_secretvalue"))
    status, state = browser.get("/api/rag/state")
    assert state["remote"]["name"] is None and "연결하지 못했어요" in state["error"]
    status, body = browser.get("/api/rag/collections")
    assert status == 400 and "omm_secretvalue" not in body["error"]
    assert not any("omm_secretvalue" in text for text in browser.seen)


# ------------------------------------------------------------ AI 연결 --


def test_connect_and_disconnect_local_then_remote(employee, browser, agents_home):
    url, people, _ = employee
    claude = agents_home / ".claude.json"
    claude.write_text('{"projects": {}}\n', encoding="utf-8")
    assert browser.post("/api/rag/connect", {"harness": "nope"})[0] == 400
    assert browser.post("/api/rag/connect", {"harness": "claude-code"}) == (200, {"harness": "claude-code", "state": "added", "remote": False})
    row = next(r for r in browser.get("/api/rag/state")[1]["harnesses"] if r["harness"] == "claude-code")
    assert row["registered"] and row["managed"] and not row["remote"] and row["current"]
    entry = json.loads(claude.read_text(encoding="utf-8"))["mcpServers"]["omm-documents"]
    assert entry["type"] == "stdio"
    # In remote mode the same button points the agent at the head's /mcp.
    browser.post("/api/rag/remote/set", {"url": url, "token": people["kim"]})
    assert browser.post("/api/rag/connect", {"harness": "claude-code"})[1]["state"] == "updated"
    status, state = browser.get("/api/rag/state")
    row = next(r for r in state["harnesses"] if r["harness"] == "claude-code")
    assert row["remote"] is True
    assert people["kim"] not in json.dumps(state)
    assert json.loads(claude.read_text(encoding="utf-8"))["mcpServers"]["omm-documents"]["url"] == url + "/mcp"
    assert browser.post("/api/rag/disconnect", {"harness": "claude-code", "confirmed": True})[1]["state"] == "restored"
    assert claude.read_text(encoding="utf-8") == '{"projects": {}}\n'
    assert browser.post("/api/rag/disconnect", {"harness": "claude-code", "confirmed": True})[1]["state"] == "not_connected"
    assert not any(people["kim"] in text for text in browser.seen)
