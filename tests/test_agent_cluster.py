from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omm import agent_cluster, agent_connection as agent, cli, rag_remote

TOKEN = "omm_employee-secret"


@pytest.fixture
def home(tmp_path, monkeypatch, isolated_omm_home):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agent_cluster.shutil, "which", lambda name: "/installed/" + name)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/installed/" + name)
    return root


@pytest.fixture
def gateway():
    """A stand-in for the head's gateway: token check plus exo's /v1/models shape."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"object": "list", "data": [
                {"id": "mlx-community/Qwen3.5-9B-4bit", "context_length": 262144},
                {"id": "mlx-community/Tiny-4bit", "context_length": 4096},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def remote(gateway):
    url, _ = gateway
    rag_remote.save(rag_remote.Remote(url, TOKEN))
    return url


@pytest.mark.parametrize("harness", agent.HARNESSES)
def test_cluster_connection_restores_with_agent_cloud(harness, home, remote):
    paths = agent._paths(harness)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'model = "cloud"\n' if path.suffix == ".toml" else b'{"theme": "light"}\n')
        path.chmod(0o644)
    original = [p.read_bytes() for p in paths]

    metadata = agent_cluster.connect(harness, "mlx-community/Qwen3.5-9B-4bit")

    assert metadata["base_url"] == remote and metadata["context_length"] == 262144
    assert TOKEN not in json.dumps(metadata)
    written = b"".join(p.read_bytes() for p in paths)
    assert TOKEN.encode() in written and remote.encode() in written
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)
    assert agent.status(harness)["state"] == "local"
    assert agent.restore(harness)
    assert [p.read_bytes() for p in paths] == original
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in paths)


def test_each_harness_gets_the_gateway_and_token(home, remote):
    agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    agent_cluster.connect("codex", "mlx-community/Qwen3.5-9B-4bit")
    agent_cluster.connect("pi", "mlx-community/Qwen3.5-9B-4bit")

    claude = json.loads(agent._paths("claude")[0].read_text())
    assert claude["env"]["ANTHROPIC_BASE_URL"] == remote
    assert claude["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert claude["model"] == "mlx-community/Qwen3.5-9B-4bit"
    codex = agent._paths("codex")[0].read_text()
    assert f'base_url = "{remote}/v1"' in codex
    assert f'experimental_bearer_token = "{TOKEN}"' in codex
    assert 'wire_api = "responses"' in codex
    models = json.loads(agent._paths("pi")[0].read_text())
    assert models["providers"]["omm-local"]["baseUrl"] == remote + "/v1"
    assert models["providers"]["omm-local"]["apiKey"] == TOKEN
    assert models["providers"]["omm-local"]["models"][0]["contextWindow"] == 262144


def test_without_a_saved_head_nothing_is_written(home):
    with pytest.raises(agent.ConnectionError, match="omm rag remote set"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    assert not agent._paths("claude")[0].exists()
    assert not agent._journal("claude").exists()


@pytest.mark.parametrize("model, message", [
    ("mlx-community/Unknown-4bit", "does not know"),
    ("mlx-community/Tiny-4bit", "32768"),
    ("../etc", "exo model id"),
])
def test_bad_models_are_refused_before_writing(home, remote, model, message):
    with pytest.raises(agent.ConnectionError, match=message):
        agent_cluster.connect("claude", model)
    assert not agent._journal("claude").exists()


def test_rejected_token_tells_how_to_fix_it(home, gateway):
    url, _ = gateway
    rag_remote.save(rag_remote.Remote(url, "omm_revoked"))
    with pytest.raises(agent.ConnectionError, match="rejected the token") as caught:
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    assert "omm_revoked" not in str(caught.value)


def test_unreachable_head_is_reported(home, isolated_omm_home):
    rag_remote.save(rag_remote.Remote("http://127.0.0.1:9", TOKEN))
    with pytest.raises(agent.ConnectionError, match="Could not reach"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")


def test_existing_connection_is_never_replaced(home, remote):
    agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    with pytest.raises(agent.ConnectionError, match="already exists"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")


def test_cli_connects_without_printing_the_token(home, remote):
    result = CliRunner().invoke(cli.app, ["setting", "agent", "cluster", "pi", "--model", "mlx-community/Qwen3.5-9B-4bit"])
    assert result.exit_code == 0, result.output
    assert "Saved cluster routing settings for pi" in result.output
    assert TOKEN not in result.output
    restored = CliRunner().invoke(cli.app, ["setting", "agent", "cloud", "pi"])
    assert restored.exit_code == 0 and "Original routing restored" in restored.output


def test_cli_error_exit_code(home):
    result = CliRunner().invoke(cli.app, ["setting", "agent", "cluster", "claude", "--model", "m"])
    assert result.exit_code == 1
    assert "omm rag remote set" in result.output
