"""Semantic search readiness (#534): states, per-OS hints, CLI, and the head's view for employees."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

import pytest
from typer.testing import CliRunner

from omm import cli, rag_readiness, rag_remote
from omm.cluster import gateway, tokens
from omm.documents import DocumentStore

from tests import _gateway_tls

runner = CliRunner()


def _closed_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def ollama(monkeypatch):
    """A loopback stand-in for Ollama's /api/tags; set ``.tags`` to what it answers."""
    from omm import document_embeddings, documents
    monkeypatch.setattr(documents, "_embedder", document_embeddings.OllamaEmbedder)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(server.tags).encode()
            self.send_response(200 if self.path == "/api/tags" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.tags = {"models": [{"name": "bge-m3:latest", "model": "bge-m3:latest"}]}
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """Pretend OS and install layout; nothing reads the real system."""
    state = {"system": "Linux", "binary": None, "app": False, "brew": False, "units": (False, False, False)}
    monkeypatch.setattr(rag_readiness, "_system", lambda: state["system"])
    monkeypatch.setattr(rag_readiness, "_binary", lambda: state["binary"])
    monkeypatch.setattr(rag_readiness, "_mac_app", lambda: state["app"])
    monkeypatch.setattr(rag_readiness, "_brew_available", lambda: state["brew"])
    monkeypatch.setattr(rag_readiness, "_linux_units", lambda: state["units"])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    state["home"] = home
    return state


def _check(url):
    return rag_readiness.check("bge-m3", url)


# ----------------------------------------------------------------- states --


def test_ready(ollama, machine):
    machine["binary"] = Path("/usr/bin/ollama")
    machine["units"] = (True, True, False)
    result = _check(ollama.url)
    assert result["state"] == "ready" and result["ready"] is True
    assert result["command"] is None and result["persistence"] is None
    assert rag_readiness.line(result) == "의미 검색: 준비됨 (Ollama · bge-m3)"


def test_model_missing(ollama, machine):
    ollama.tags = {"models": [{"name": "nomic-embed-text:latest"}]}
    machine["binary"] = Path("/usr/bin/ollama")
    machine["units"] = (True, True, False)
    result = _check(ollama.url)
    assert result["state"] == "model_missing" and result["command"] == "ollama pull bge-m3"
    assert rag_readiness.line(result).startswith("의미 검색: 꺼짐 (임베딩 모델 없음) - Ollama에 임베딩 모델 bge-m3")
    assert rag_readiness.line(result).endswith("지금은 단어 검색만 해요.")


def test_unexpected_answer_is_error(ollama, machine):
    ollama.tags = {"models": "nope"}
    result = _check(ollama.url)
    assert result["state"] == "error" and result["ready"] is False and result["command"] is None


def test_not_running_macos_app(machine):
    machine.update(system="Darwin", app=True)
    result = _check(_closed_url())
    assert result["state"] == "not_running" and result["command"] == "open -a Ollama"
    assert result["persistence"] is None  # Ollama.app keeps its own login item.


def test_not_running_macos_homebrew(machine):
    machine.update(system="Darwin", binary=Path("/opt/homebrew/bin/ollama"))
    result = _check(_closed_url())
    assert result["state"] == "not_running" and result["command"] == "brew services start ollama"
    assert "재부팅 뒤에도 켜져요" in result["message"] and "`ollama serve`" in result["message"]
    assert result["persistence"] is None  # The start command already persists.


def test_macos_homebrew_ready_without_brew_services_suggests_persistence(ollama, machine):
    machine.update(system="Darwin", binary=Path("/opt/homebrew/bin/ollama"))
    result = _check(ollama.url)
    assert result["state"] == "ready"
    assert "brew services start ollama" in result["persistence"] and "ollama serve" in result["persistence"]
    agents = machine["home"] / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "homebrew.mxcl.ollama.plist").write_text("<plist/>", encoding="utf-8")
    assert _check(ollama.url)["persistence"] is None


def test_not_running_windows(machine, tmp_path):
    machine.update(system="Windows", binary=Path("C:/Users/me/AppData/Local/Programs/Ollama/ollama.exe"))
    result = _check(_closed_url())
    assert result["state"] == "not_running" and result["command"] == "ollama serve"
    assert "시작 메뉴에서 Ollama 앱을 실행" in result["message"]
    assert "시작 프로그램" in result["persistence"]
    startup = tmp_path / "appdata" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    (startup / "Ollama.lnk").write_bytes(b"")
    assert _check(_closed_url())["persistence"] is None


@pytest.mark.parametrize("units, command, persistence", [
    ((True, False, False), "sudo systemctl start ollama", "sudo systemctl enable ollama"),
    ((True, True, False), "sudo systemctl start ollama", None),
    ((False, False, True), "systemctl --user start ollama", None),
    ((False, False, False), "ollama serve", "systemd"),
])
def test_not_running_linux(machine, units, command, persistence):
    machine.update(binary=Path("/usr/local/bin/ollama"), units=units)
    result = _check(_closed_url())
    assert result["state"] == "not_running" and result["command"] == command
    if persistence is None:
        assert result["persistence"] is None
    else:
        assert persistence in result["persistence"]


@pytest.mark.parametrize("system, brew, command, text", [
    ("Darwin", True, "brew install ollama", "brew services start ollama"),
    ("Darwin", False, None, "https://ollama.com/download/mac"),
    ("Windows", False, None, "https://ollama.com/download/windows"),
    ("Linux", False, None, "https://ollama.com/download/linux"),
])
def test_not_installed(machine, system, brew, command, text):
    machine.update(system=system, brew=brew)
    result = _check(_closed_url())
    assert result["state"] == "not_installed" and result["command"] == command
    assert text in result["message"] and "`ollama pull bge-m3`" in result["message"]
    assert result["persistence"] is None


def test_homebrew_detection_follows_the_real_path():
    assert rag_readiness._from_homebrew(Path("/opt/homebrew/bin/ollama"))
    assert rag_readiness._from_homebrew(Path("/usr/local/Cellar/ollama/0.12.0/bin/ollama"))
    assert not rag_readiness._from_homebrew(Path("/usr/bin/ollama"))
    assert not rag_readiness._from_homebrew(None)


# -------------------------------------------------------------------- CLI --


def test_rag_status_shows_the_line_and_json(isolated_omm_home, machine):
    DocumentStore().set_embedding_model("bge-m3", base_url=_closed_url())
    machine.update(system="Darwin", app=True)
    result = runner.invoke(cli.app, ["rag", "status"])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "의미 검색: 꺼짐 (Ollama가 꺼져 있음) - Ollama가 설치돼 있지만 켜져 있지 않아요. `open -a Ollama`로" in output
    data = json.loads(runner.invoke(cli.app, ["rag", "status", "--json"]).output)
    assert data["semantic_search"]["state"] == "not_running"
    assert data["semantic_search"]["command"] == "open -a Ollama"
    assert "remote" not in data


# ------------------------------------------------- the head, for employees --


@pytest.fixture
def head(isolated_omm_home, machine):
    DocumentStore().set_embedding_model("bge-m3", base_url=_closed_url())
    machine["binary"] = Path("/usr/local/bin/ollama")
    token = tokens.add_token("kim")
    server = gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server, token
    server.shutdown()
    server.server_close()


@pytest.fixture
def employee(head, tmp_path, monkeypatch):
    server, token = head
    url = _gateway_tls.url(server)
    monkeypatch.setattr(rag_remote, "remote_path", lambda: tmp_path / "employee-rag-remote.json")
    _gateway_tls.pin(server)
    rag_remote.save(rag_remote.Remote(url, token))
    return url, token


def test_head_readiness_endpoint_shares_only_state_and_model(employee):
    url, token = employee
    data = rag_remote.Client(rag_remote.Remote(url, token)).call("GET", "/rag/v1/readiness")
    assert data == {"state": "not_running", "model": "bge-m3"}
    with pytest.raises(rag_remote.RemoteError) as caught:
        rag_remote.Client(rag_remote.Remote(url, "omm_wrong")).call("GET", "/rag/v1/readiness")
    assert caught.value.status == 401


def test_employee_sees_the_head_state_in_remote_and_status(employee):
    url, _ = employee
    expected = "회사 head의 의미 검색이 꺼져 있어요(Ollama가 꺼져 있음) - 관리자에게 알려 주세요. 지금은 단어 검색만 해요."
    result = runner.invoke(cli.app, ["rag", "remote"])
    assert result.exit_code == 0, result.output
    assert expected in " ".join(result.output.split())
    output = " ".join(runner.invoke(cli.app, ["rag", "status"]).output.split())
    assert f"원격 헤드: {url}" in output and expected in output
    assert "systemctl" not in output.split("아래는")[0]  # No admin commands in the head part.
    data = json.loads(runner.invoke(cli.app, ["rag", "status", "--json"]).output)
    assert data["remote"] == {"url": url, "semantic_search": {"state": "not_running", "model": "bge-m3"}}


def test_employee_view_of_a_ready_head(employee, ollama):
    DocumentStore().set_embedding_model("bge-m3", base_url=ollama.url)
    output = " ".join(runner.invoke(cli.app, ["rag", "remote"]).output.split())
    assert "회사 head의 의미 검색: 준비됨 (bge-m3)" in output


class _Client:
    def __init__(self, error=None, data=None):
        self.error, self.data = error, data

    def call(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.data


def test_older_or_unreachable_head():
    old = rag_readiness.remote_view(_Client(rag_remote.RemoteError("x", 404)))
    assert old == {"state": "unsupported", "model": None}
    assert "지원하지 않아요" in rag_readiness.remote_line(old)
    down = rag_readiness.remote_view(_Client(rag_remote.RemoteError("x")))
    assert down["state"] == "unreachable" and "연결되는지" in rag_readiness.remote_line(down)
    assert rag_readiness.remote_view(_Client(data={"state": "weird"}))["state"] == "unsupported"
