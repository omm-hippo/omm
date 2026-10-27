"""Real loopback HTTP covers the employee flow without contacting a live cluster."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import uuid

import pytest
import requests

from omm import rag_remote
from omm.cluster import tls
from omm.web import shared_ai
from omm.web.server import WebServer


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    class Gateway(BaseHTTPRequestHandler):
        calls = []
        status = 200
        invalid = False
        response_model = None
        invalid_chat = False
        def log_message(self, *_):
            pass
        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        def do_GET(self):
            self.calls.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer employee-secret":
                self.send_error(401)
                return
            self.respond({"not_models": True} if self.invalid else {"data": [{"id": "team/test"}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.calls.append(("POST", self.path, body))
            self.respond([] if self.invalid_chat else {"model": self.response_model or body["model"], "choices": [{"message": {"content": "56"}, "finish_reason": "stop"}]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    # HTTPS from a head CA that `omm rag remote set` already pinned (#536).
    certificates = tls.HeadCertificates(tmp_path / "head-tls", names=lambda: ["10.42.0.2", "10.42.0.3"])
    server.socket = certificates.context().wrap_socket(server.socket, server_side=True)
    rag_remote.save_ca(certificates.ca_pem().decode())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Use a real socket while keeping loopback unavailable in production URLs.
    monkeypatch.setattr(shared_ai, "private_address", lambda *_: "127.0.0.1")
    yield f"https://10.42.0.2:{server.server_port}", Gateway
    server.shutdown()
    server.server_close()
    thread.join(5)


@pytest.fixture
def app(isolated_omm_home, tmp_path):
    assets = tmp_path / "static"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>OMM test</title>", encoding="utf-8")
    server = WebServer(0, static_root=assets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = requests.Session()
    assert client.get(server.url, timeout=3).status_code == 200
    client.headers["X-OMM-Web"] = "1"
    yield server, client
    client.close()
    server.shutdown()
    server.server_close()
    thread.join(5)


def connect(server, client, url, token="employee-secret"):
    return client.post(server.url + "api/shared-ai/connect", json={"url": url, "token": token}, timeout=3)


def question(connection, **changes):
    return {"connection_id": connection["connection_id"], "model": "team/test",
            "messages": [{"role": "user", "content": "7*8"}], "request_id": str(uuid.uuid4()), **changes}


def test_real_http_connect_persist_ask_restore_and_clear(app, gateway):
    server, client = app
    url, remote = gateway
    assert client.get(server.url + "api/shared-ai").json() == {"configured": False, "busy": False}
    result = connect(server, client, url)
    assert result.status_code == 200
    connection = result.json()
    assert connection["models"] == ["team/test"]
    assert connection["readiness"] == "not_checked"
    assert "employee-secret" not in result.text
    assert shared_ai.SharedAI().state() == connection
    path = server.shared_ai.path
    assert "employee-secret" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    reply = client.post(server.url + "api/shared-ai/messages", json=question(connection), timeout=3)
    assert reply.status_code == 200 and reply.json()["text"] == "56"
    assert reply.json()["response_verified"] is True
    sent = remote.calls[-1][2]
    assert sent["enable_thinking"] is False and sent["max_tokens"] == 512
    assert "7*8" not in path.read_text(encoding="utf-8") and "messages" not in json.loads(path.read_text(encoding="utf-8"))
    assert server.shared_ai.state()["model"] == "team/test"
    cleared = client.post(server.url + "api/shared-ai/clear", json={"confirmed": True}, timeout=3)
    assert cleared.status_code == 200 and not path.exists()
    assert len(remote.calls) == 2


def test_same_origin_and_confirmation_boundaries(app, gateway):
    server, client = app
    url, remote = gateway
    assert requests.get(server.url + "api/shared-ai").status_code == 401
    assert requests.post(server.url + "api/shared-ai/connect", json={"url": url, "token": "secret"}).status_code == 401
    assert client.post(server.url + "api/shared-ai/connect", headers={"Origin": "http://evil.invalid"}, json={}).status_code == 403
    assert client.post(server.url + "api/shared-ai/clear", json={}).status_code == 400
    assert remote.calls == []


def test_bad_token_has_action_and_does_not_replace_existing_connection(app, gateway):
    server, client = app
    url, _ = gateway
    before = connect(server, client, url).json()
    raw = server.shared_ai.path.read_text(encoding="utf-8")
    response = connect(server, client, url, "private-wrong-token")
    assert response.status_code == 400 and response.json()["code"] == "token"
    assert "private-wrong-token" not in response.text and "action" in response.json()
    assert server.shared_ai.path.read_text(encoding="utf-8") == raw
    assert server.shared_ai.state()["connection_id"] == before["connection_id"]


def test_saved_token_reused_only_for_same_destination(app, gateway):
    server, client = app
    url, _ = gateway
    assert connect(server, client, url).status_code == 200
    assert connect(server, client, url, "").status_code == 200
    response = connect(server, client, "https://10.42.0.3:52440", "")
    assert response.json()["code"] == "token"


def test_duplicate_question_is_not_generated_twice_and_old_tab_is_rejected(app, gateway):
    server, client = app
    url, remote = gateway
    connection = connect(server, client, url).json()
    body = question(connection)
    first = client.post(server.url + "api/shared-ai/messages", json=body).json()
    second = client.post(server.url + "api/shared-ai/messages", json=body).json()
    assert first == second and len(remote.calls) == 2
    body["messages"][0]["content"] = "different"
    assert client.post(server.url + "api/shared-ai/messages", json=body).status_code == 409
    connect(server, client, url)
    assert client.post(server.url + "api/shared-ai/messages", json=question(connection)).json()["code"] == "changed"


@pytest.mark.parametrize("status,code", [(301,"server"),(404,"protocol"),(429,"quota"),(503,"model"),(504,"timeout")])
def test_server_status_is_classified_without_retry_or_echo(app, gateway, status, code):
    server, client = app
    url, remote = gateway
    remote.status = status
    response = connect(server, client, url)
    assert response.json()["code"] == code
    assert len(remote.calls) == 1 and not server.shared_ai.path.exists()


def test_html_or_non_catalog_response_is_not_a_success(app, gateway):
    server, client = app
    url, remote = gateway
    remote.invalid = True
    assert connect(server, client, url).json()["code"] == "protocol"
    assert not server.shared_ai.path.exists()


@pytest.mark.parametrize("value", [None, True, "", "http://user:secret@10.0.0.1", "http://10.0.0.1/path",
    "http://10.0.0.1?token=secret", "http://10.0.0.1:0", "file:///tmp/token", "http://[::1%lo0]", "http://10.0.0.1\\other"])
def test_rejects_ambiguous_or_credential_bearing_addresses(value):
    with pytest.raises(shared_ai.SharedError):
        shared_ai.normalize_url(value)


@pytest.mark.parametrize("addresses,allowed", [(["100.110.168.49"],True),(["192.168.0.4"],True),
    (["fd7a:115c:a1e0::1"],True),(["127.0.0.1"],False),(["169.254.169.254"],False),
    (["8.8.8.8"],False),(["10.0.0.1","8.8.8.8"],False),(["::1"],False),(["0.0.0.0"],False)])
def test_dns_results_all_must_be_private_and_unicast(monkeypatch, addresses, allowed):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0,0,0,"",(ip,52440)) for ip in addresses])
    if allowed:
        assert shared_ai.private_address("team.ts.net",52440) == addresses[0]
    else:
        with pytest.raises(shared_ai.SharedError, match="주소"):
            shared_ai.private_address("team.ts.net",52440)


def test_connection_refused_does_not_claim_tailscale_is_the_cause(app, gateway, monkeypatch):
    server, client = app
    url, _ = gateway
    def refused(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(socket, "create_connection", refused)
    # Call the domain method: patching sockets also blocks the local API client.
    with pytest.raises(shared_ai.SharedError) as error:
        server.shared_ai.connect({"url": url, "token": "employee-secret"})
    assert error.value.code == "network"
    assert "아직 확인되지" in error.value.action


def test_symlink_settings_do_not_read_or_overwrite_other_files(app, tmp_path, requires_symlink_support):
    server, _ = app
    target = tmp_path / "outside.json"
    target.write_text("private", encoding="utf-8")
    server.shared_ai.path.parent.mkdir()
    server.shared_ai.path.symlink_to(target)
    with pytest.raises(shared_ai.SharedError):
        server.shared_ai.state()
    assert target.read_text(encoding="utf-8") == "private"


def test_busy_and_malformed_history_never_send_a_request(app, gateway):
    server, client = app
    url, remote = gateway
    connection = connect(server, client, url).json()
    server.shared_ai.busy = True
    assert client.post(server.url + "api/shared-ai/messages", json=question(connection)).status_code == 409
    server.shared_ai.busy = False
    for messages in [[{"role":"system","content":"no"}], [], [{"role":"user","content":"x"*8193}]]:
        assert client.post(server.url + "api/shared-ai/messages", json=question(connection,messages=messages)).status_code == 400
    assert len(remote.calls) == 1


def test_failed_question_is_cached_without_automatic_resend(app, gateway):
    server, client = app
    url, remote = gateway
    connection = connect(server, client, url).json()
    remote.status = 503
    body = question(connection)
    assert client.post(server.url + "api/shared-ai/messages", json=body).json()["code"] == "model"
    assert client.post(server.url + "api/shared-ai/messages", json=body).json()["code"] == "model"
    assert len(remote.calls) == 2


def test_corrupt_connection_can_be_cleared_without_network(app):
    server, client = app
    server.shared_ai.path.parent.mkdir()
    server.shared_ai.path.write_text("invalid-json", encoding="utf-8")
    response = client.get(server.url + "api/shared-ai")
    assert response.json()["code"] == "storage"
    assert client.post(server.url + "api/shared-ai/clear", json={"confirmed": True}).json()["configured"] is False


@pytest.mark.parametrize("invalid_chat", [False, True])
def test_other_model_response_is_not_labeled_as_selected_model_success(app, gateway, invalid_chat):
    server, client = app
    url, remote = gateway
    connection = connect(server, client, url).json()
    remote.response_model = "other/model"
    remote.invalid_chat = invalid_chat
    response = client.post(server.url + "api/shared-ai/messages", json=question(connection))
    assert response.json()["code"] == "protocol"
    assert "response_verified" not in response.json()


def test_catalog_byte_limit_covers_multibyte_model_identifiers():
    rows = [{"id": str(i) + "가" * 240} for i in range(500)]
    with pytest.raises(shared_ai.SharedError):
        shared_ai.model_ids({"data": rows})


def test_partial_connection_metadata_is_recoverable_storage_error(app, gateway):
    server, client = app
    url, _ = gateway
    assert connect(server, client, url).status_code == 200
    path = server.shared_ai.path
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["checked_at"]
    path.write_text(json.dumps(value), encoding="utf-8")
    assert client.get(server.url + "api/shared-ai").json()["code"] == "storage"
    assert client.post(server.url + "api/shared-ai/clear", json={"confirmed": True}).status_code == 200


def test_plain_http_is_refused_before_sending_the_token(app, gateway):
    server, client = app
    url, remote = gateway
    response = connect(server, client, url.replace("https://", "http://")).json()
    assert response["code"] == "plain" and "https://" in response["action"]
    assert remote.calls == []


def test_without_a_pinned_certificate_the_terminal_check_comes_first(app, gateway):
    server, client = app
    url, remote = gateway
    rag_remote.ca_path().unlink()
    response = connect(server, client, url).json()
    assert response["code"] == "tls" and "omm rag remote set https://" in response["action"]
    assert remote.calls == []


def test_a_server_with_another_certificate_is_refused(app, gateway, tmp_path):
    server, client = app
    url, remote = gateway
    tls.ensure(tmp_path / "other", ["10.42.0.2"])
    rag_remote.save_ca((tmp_path / "other" / "ca.pem").read_text(encoding="utf-8"))
    assert connect(server, client, url).json()["code"] == "tls"
    assert remote.calls == []
