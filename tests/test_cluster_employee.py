"""Read-only cluster commands on an employee PC (token via `omm rag remote set`).

A real GatewayServer runs in front of a fake exo that answers with the
captured exo 1.0.71 fixtures, so the requests cross the same HTTP boundary
an employee's do. The employee side runs as if on Linux to prove it never
needs a local exo or macOS.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform
import threading
from urllib.parse import urlsplit

import pytest
from typer.testing import CliRunner

from omm import cli, rag_remote
from omm.cluster import chat, gateway, tokens
from tests import _gateway_tls

FIX = Path(__file__).parent / "fixtures" / "exo"
LOADED = "mlx-community/Llama-3.2-1B-Instruct-4bit"
STREAM = (FIX / "chat-stream-stats.txt").read_bytes()
runner = CliRunner()


def _json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class _Exo(ThreadingHTTPServer):
    previews = _json("previews.json")

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _ExoHandler)
        self.state = _json("state-1node.json")
        # The loaded model is also downloaded on the one Mac (Completed entry from a live exo).
        node = next(iter(self.state["downloads"]))
        completed = next(w for w in next(iter(_json("downloads-mixed.json")["downloads"].values()))
                         if "DownloadCompleted" in w)
        self.state["downloads"][node].append(completed)


class _ExoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        replies = {"/state": self.server.state, "/models": _json("models.json"),
                   "/instance/previews": self.server.previews, "/models/search": _json("search.json")}
        self._send(json.dumps(replies.get(path, {})).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(STREAM, "text/event-stream")


def _serve(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def employee(isolated_omm_home, monkeypatch):
    exo = _serve(_Exo())
    token = tokens.add_token("kim")
    head = _serve(gateway.GatewayServer("127.0.0.1", 0, upstream=f"http://127.0.0.1:{exo.server_address[1]}"))
    url = _gateway_tls.url(head)
    rag_remote.save(rag_remote.Remote(url, token))
    _gateway_tls.pin(head)
    yield url
    head.shutdown()
    exo.shutdown()
    head.server_close()
    exo.server_close()


def _run(*args):
    result = runner.invoke(cli.app, list(args))
    return result, " ".join(result.output.split())


def test_status_shows_macs_and_loaded_models_not_admin_details(employee):
    result, text = _run("cluster", "status")
    assert result.exit_code == 0, result.output
    assert f"Cluster head: {employee}" in text
    assert "Macs connected: 1" in text
    assert "Loaded models:" in text and LOADED in text
    assert "Firewall" not in text and "mac-a" not in text and "RDMA" not in text


def test_status_json_for_an_employee(employee):
    result = runner.invoke(cli.app, ["--json", "cluster", "status"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["role"] == "employee" and data["macs"] == 1
    assert data["memory_total_bytes"] == 68719476736
    assert [row["model"] for row in data["models"]] == [LOADED]


def test_info_counts_macs_without_naming_them(employee):
    result, text = _run("cluster", "info", LOADED)
    assert result.exit_code == 0, result.output
    assert "Macs: 1/1 installed" in text and "mac-a" not in text
    assert "Loaded" in text


def test_info_lists_placements_by_mac_count_only(employee, monkeypatch):
    server_previews = _json("previews-3node.json")
    monkeypatch.setattr(_Exo, "previews", server_previews, raising=False)
    result = runner.invoke(cli.app, ["--json", "cluster", "info", "mlx-community/Qwen3-0.6B-4bit"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["placements"]
    assert len(rows) == 9 and all("nodes" not in row for row in rows)
    result, text = _run("cluster", "info", "mlx-community/Qwen3-0.6B-4bit")
    assert "Tensor RDMA 2 Macs" in text and "omm's pick" in text and "12D3KooW" not in text


def test_info_of_an_unknown_model_shows_the_heads_reason(employee):
    result, text = _run("cluster", "info", "nobody/Nothing")
    assert result.exit_code == 1
    assert "omm cluster search" in text


def test_recommend_runs_on_the_head(employee):
    result, text = _run("cluster", "recommend")
    assert result.exit_code == 0, result.output
    assert "mlx-community/" in text
    assert "ask the cluster admin" in text


def test_search_runs_on_the_head(employee):
    result, text = _run("cluster", "search", "qwen3")
    assert result.exit_code == 0, result.output
    assert "mlx-community/Qwen3-0.6B-8bit" in text


def test_benchmark_measures_a_loaded_model_through_the_gateway(employee):
    result, text = _run("cluster", "benchmark", LOADED)
    assert result.exit_code == 0, result.output
    assert "tok/s" in text
    # Installed copies are not the Macs serving it (Tensor can use 2 of 3), so no count is claimed.
    assert "Mac(s)" not in text


def test_benchmark_refuses_a_model_that_is_not_loaded(employee):
    result, text = _run("cluster", "benchmark", "mlx-community/Qwen3-0.6B-4bit")
    assert result.exit_code == 1
    assert "ask the cluster admin to run: omm cluster load mlx-community/Qwen3-0.6B-4bit" in text


@pytest.mark.parametrize("args", [("install", LOADED), ("uninstall", LOADED), ("load", LOADED), ("unload", LOADED)])
def test_admin_commands_point_at_the_head(employee, args):
    result, text = _run("cluster", *args)
    assert result.exit_code == 2
    assert "runs on the cluster's head Mac; ask its admin" in text


def test_employee_commands_never_need_macos(employee, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    for args in (("cluster", "status"), ("cluster", "list"), ("cluster", "info", LOADED)):
        result, text = _run(*args)
        assert result.exit_code == 0, (args, result.output)


def _pem(home) -> str:
    from omm.cluster import tls

    tls.ensure(home / "ca", ["127.0.0.1"])
    return (home / "ca" / "ca.pem").read_text(encoding="utf-8")


def test_an_older_head_says_to_update_it(isolated_omm_home, monkeypatch):
    from urllib import error as urlerror

    rag_remote.save(rag_remote.Remote("https://192.0.2.20:52440", "omm_x"))
    rag_remote.save_ca(_pem(isolated_omm_home))

    def old_head(self, request, timeout=None):
        raise urlerror.HTTPError(request.full_url, 404, "Not Found", {}, None)

    import urllib.request

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", old_head)
    result, text = _run("cluster", "status")
    assert result.exit_code == 1
    assert "runs an older omm" in text
