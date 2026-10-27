"""The head gateway's `/omm/member/*` routes: member proofs, reports, snapshots, handover."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import ssl
import threading
import time

import pytest

from omm.cluster import gateway, member_auth, members, rag_backup, settings, tokens, version_follow
from omm.documents import DocumentStore

NS = "omm-" + "a" * 32
KEY = member_auth.member_key(NS)
HEAD_VERSION = {"commit": "b" * 40, "branch": "main", "git": True}
REPORT = {"version": HEAD_VERSION, "version_state": "same", "version_error": None, "rag_marker": None,
          "prepared": True, "documents_ready": True}


@pytest.fixture
def server(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(version_follow, "local_version", lambda: dict(HEAD_VERSION))
    settings.save(settings.ClusterSettings("head", NS, gateway.GATEWAY_PORT, head_epoch=3))
    srv = gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9")
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _blind():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def call(srv, method, path, *, node="12D3member", key=KEY, nonce=None, body=None, headers=None, leaf=None,
         signed_body=None):
    """A member's request; returns (status, body, headers, leaf sha256, nonce)."""
    nonce = nonce or f"n{time.monotonic_ns()}"
    conn = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], timeout=10, context=_blind())
    conn.connect()
    seen = member_auth.leaf_sha256(conn.sock.getpeercert(binary_form=True))
    body_sha = hashlib.sha256(signed_body if signed_body is not None else body or b"").hexdigest()
    signed = member_auth.sign_request(key, method, path, node, now=time.time(), nonce=nonce,
                                      leaf_sha256=leaf or seen, body_sha256=body_sha) if key else {}
    conn.request(method, path, body=body, headers={**signed, **(headers or {})})
    leaf = seen
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data, dict(resp.getheaders()), leaf, nonce


def poll(srv, node="12D3member", report=REPORT, **kw):
    return call(srv, "POST", "/omm/member/poll", node=node, body=json.dumps(report).encode(), **kw)


def proven(data, headers, leaf, nonce, body_sha=None):
    return member_auth.verify_head(KEY, nonce, leaf, body_sha or hashlib.sha256(data).hexdigest(),
                                   headers.get(member_auth.HEADER_HEAD_PROOF))


def test_settings_head_epoch_round_trip_and_default(isolated_omm_home):
    settings.save(settings.ClusterSettings("head", NS, 52440, head_epoch=7))
    assert settings.load().head_epoch == 7
    raw = json.loads(settings.settings_path().read_text(encoding="utf-8"))
    del raw["head_epoch"]
    settings.settings_path().write_text(json.dumps(raw), encoding="utf-8")
    assert settings.load().head_epoch == 0


def test_member_poll_records_report_and_proves_reply(server):
    status, data, headers, leaf, nonce = poll(server)
    assert status == 200
    body = json.loads(data)
    assert body["head"] is True and body["epoch"] == 3 and body["version"] == HEAD_VERSION
    assert body["rag_marker"] is None and body["handover"] is None
    assert proven(data, headers, leaf, nonce)
    record = members.load()["12D3member"]
    assert record["version_state"] == "same" and record["seen_at"]


def test_member_route_rejects_employee_token(server):
    token = tokens.add_token("kim")
    status, *_ = call(server, "POST", "/omm/member/poll", key=None, body=b"{}",
                      headers={"Authorization": f"Bearer {token}"})
    assert status == 401
    assert members.load() == {}


def test_member_route_rejects_other_cluster(server):
    status, *_ = poll(server, key=member_auth.member_key("omm-" + "b" * 32))
    assert status == 401


def test_member_headers_do_not_open_employee_routes(server):
    status, *_ = call(server, "GET", "/omm/status")
    assert status == 401


def test_replayed_member_request_rejected(server):
    assert poll(server, nonce="same")[0] == 200
    assert poll(server, nonce="same")[0] == 401


def test_poll_body_must_be_a_json_object(server):
    assert call(server, "POST", "/omm/member/poll", body=b"[1]")[0] == 400
    assert call(server, "POST", "/omm/member/poll", body=b"x" * (64 * 1024 + 1))[0] == 413


def test_handover_instruction_only_for_target(server):
    members.start_handover("12D3target", 4, {"models": [], "known_nodes": ["a"], "gateway_port": 52440}, "m1",
                           now=datetime.now(timezone.utc))
    body = json.loads(poll(server, node="12D3target")[1])
    assert body["handover"]["to"] == "12D3target" and body["handover"]["epoch"] == 4
    assert body["handover"]["rag_marker"] == "m1"
    assert json.loads(poll(server, node="12D3other")[1])["handover"] is None


def test_expired_handover_is_not_pending(isolated_omm_home):
    members.start_handover("x", 2, {}, None, now=datetime.now(timezone.utc) - timedelta(seconds=301))
    assert members.pending_handover() is None


def test_snapshot_route_streams_backup_with_marker(server):
    DocumentStore().create_collection("company")
    status, data, headers, leaf, nonce = call(server, "GET", "/omm/member/rag/snapshot")
    assert status == 200
    sha = headers["X-Omm-Rag-Sha256"]
    assert hashlib.sha256(data).hexdigest() == sha
    assert headers["X-Omm-Rag-Marker"] == rag_backup.store_marker()
    assert proven(data, headers, leaf, nonce, sha)


def test_snapshot_404_without_store(server):
    assert call(server, "GET", "/omm/member/rag/snapshot")[0] == 404


def test_poll_reports_the_store_marker(server):
    DocumentStore().create_collection("company")
    assert json.loads(poll(server)[1])["rag_marker"] == rag_backup.store_marker()


def test_rag_writes_refused_during_handover(server):
    token = tokens.add_token("kim")
    members.start_handover("12D3target", 4, {}, None, now=datetime.now(timezone.utc))
    conn = http.client.HTTPSConnection("127.0.0.1", server.server_address[1], timeout=10, context=_blind())
    conn.request("POST", "/rag/collections", body=b'{"name":"x"}',
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 503 and b"moving to another Mac" in resp.read()


def test_head_proof_uses_the_leaf_of_a_chain_file(server):
    # server.pem holds the leaf and the CA; the proof is over the leaf the member sees.
    pem = (server.certificates.directory / "server.pem").read_text(encoding="ascii")
    assert pem.count("BEGIN CERTIFICATE") >= 2
    for _ in range(5):
        status, data, headers, leaf, nonce = poll(server)
        assert status == 200 and proven(data, headers, leaf, nonce)


def test_relayed_request_with_another_leaf_is_refused(server):
    # The member signed for the relay's certificate, not the head's: no snapshot leaves the head.
    DocumentStore().create_collection("company")
    status, data, *_ = call(server, "GET", "/omm/member/rag/snapshot", leaf="00" * 32)
    assert status == 401 and b"SQLite" not in data


def test_tampered_report_is_refused(server):
    status, *_ = call(server, "POST", "/omm/member/poll", body=json.dumps({**REPORT, "prepared": False}).encode(),
                      signed_body=json.dumps(REPORT).encode())
    assert status == 401 and members.load() == {}


def test_gateway_on_a_member_does_not_claim_head(server):
    current = settings.load()
    settings.save(settings.ClusterSettings("member", NS, current.gateway_port, head_epoch=current.head_epoch))
    assert json.loads(poll(server)[1])["head"] is False


def test_run_gateway_on_a_member_removes_itself(isolated_omm_home, monkeypatch):
    from omm.cluster import service

    settings.save(settings.ClusterSettings("member", NS, 52440))
    removed = []
    monkeypatch.setattr(service, "uninstall", lambda **k: removed.append(True))
    monkeypatch.setattr(gateway, "GatewayServer", lambda *a, **k: pytest.fail("a member must not serve"))
    assert gateway.run_gateway() == 0 and removed == [True]
