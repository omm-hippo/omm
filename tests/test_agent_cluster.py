from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omm import agent_cluster, agent_connection as agent, cli, rag_remote
from omm.cluster import tls

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
def gateway(isolated_omm_home, tmp_path):
    """A stand-in for the head's gateway over HTTPS: token check plus exo's /v1/models shape.

    Its CA is pinned the way `omm rag remote set` leaves it."""
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
    certificates = tls.HeadCertificates(tmp_path / "head-tls", names=lambda: ["127.0.0.1"])
    server.socket = certificates.context().wrap_socket(server.socket, server_side=True)
    rag_remote.save_ca(certificates.ca_pem().decode())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}", seen
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
    if os.name != "nt":  # POSIX file permissions
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)
    assert agent.status(harness)["state"] == "local"
    assert agent_cluster.restore(harness)
    assert [p.read_bytes() for p in paths] == original
    if os.name != "nt":
        assert all(p.stat().st_mode & 0o777 == 0o644 for p in paths)


def test_each_harness_gets_the_gateway_and_token(home, remote):
    agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    agent_cluster.connect("codex", "mlx-community/Qwen3.5-9B-4bit")
    agent_cluster.connect("pi", "mlx-community/Qwen3.5-9B-4bit")

    claude = json.loads(agent._paths("claude")[0].read_text(encoding="utf-8"))
    assert claude["env"]["ANTHROPIC_BASE_URL"] == remote
    assert claude["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert claude["model"] == "mlx-community/Qwen3.5-9B-4bit"
    codex = agent._paths("codex")[0].read_text(encoding="utf-8")
    assert f'base_url = "{remote}/v1"' in codex
    assert f'experimental_bearer_token = "{TOKEN}"' in codex
    assert 'wire_api = "responses"' in codex
    models = json.loads(agent._paths("pi")[0].read_text(encoding="utf-8"))
    assert models["providers"]["omm-local"]["baseUrl"] == remote + "/v1"
    assert models["providers"]["omm-local"]["apiKey"] == TOKEN
    assert models["providers"]["omm-local"]["models"][0]["contextWindow"] == 262144


def test_without_a_saved_head_nothing_is_written(home):
    with pytest.raises(agent.ConnectionError, match="omm rag remote set"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    assert not agent._paths("claude")[0].exists()
    assert not agent._journal("claude").exists()


@pytest.mark.parametrize("model, message", [
    ("mlx-community/Unknown-4bit", "is not loaded on the cluster"),
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


def test_unreachable_head_is_reported(home, gateway):
    rag_remote.save(rag_remote.Remote("https://127.0.0.1:9", TOKEN))
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


def test_claude_trusts_only_the_pinned_ca_and_restore_removes_it(home, remote):
    metadata = agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    path = agent._paths("claude")[0]
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["env"]["NODE_EXTRA_CA_CERTS"] == str(rag_remote.ca_path()) == metadata["ca_file"]
    assert "launch" not in metadata
    # Claude Code rewrote an unrelated setting since, so restore has to merge.
    settings["theme"] = "dark"
    path.write_text(json.dumps(settings), encoding="utf-8")
    assert agent_cluster.restore("claude")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == {"theme": "dark"}


def test_claude_keeps_a_ca_file_the_user_already_set(home, remote):
    path = agent._paths("claude")[0]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"NODE_EXTRA_CA_CERTS": "/corp/proxy.pem"}}), encoding="utf-8")
    metadata = agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    assert metadata["ca_conflict"] == "/corp/proxy.pem"
    assert json.loads(path.read_text(encoding="utf-8"))["env"]["NODE_EXTRA_CA_CERTS"] == "/corp/proxy.pem"
    assert agent_cluster.restore("claude")
    assert json.loads(path.read_text(encoding="utf-8")) == {"env": {"NODE_EXTRA_CA_CERTS": "/corp/proxy.pem"}}


@pytest.mark.parametrize("harness, variable", [("codex", "CODEX_CA_CERTIFICATE"), ("pi", "NODE_EXTRA_CA_CERTS")])
def test_codex_and_pi_get_the_line_that_starts_them_trusting_the_ca(home, remote, harness, variable):
    metadata = agent_cluster.connect(harness, "mlx-community/Qwen3.5-9B-4bit")
    assert metadata["launch"] == f"{variable}={rag_remote.ca_path()} {harness}"
    result = CliRunner().invoke(cli.app, ["setting", "agent", "cloud", harness])
    assert result.exit_code == 0


def test_cli_prints_how_to_start_pi(home, remote):
    result = CliRunner().invoke(cli.app, ["setting", "agent", "cluster", "pi", "--model", "mlx-community/Qwen3.5-9B-4bit"])
    assert result.exit_code == 0, result.output
    assert f"NODE_EXTRA_CA_CERTS={rag_remote.ca_path()} pi" in " ".join(result.output.split())


def test_a_head_saved_over_plain_http_must_be_set_again(home, isolated_omm_home):
    rag_remote.save(rag_remote.Remote("http://127.0.0.1:9", TOKEN))
    with pytest.raises(agent.ConnectionError, match="https://"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
    assert not agent._journal("claude").exists()


def test_a_head_with_another_certificate_is_refused(home, remote, tmp_path):
    other = tmp_path / "other-ca"
    tls.ensure(other, ["127.0.0.1"])
    rag_remote.save_ca((other / "ca.pem").read_text(encoding="utf-8"))
    with pytest.raises(agent.ConnectionError, match="did not show the certificate pinned"):
        agent_cluster.connect("claude", "mlx-community/Qwen3.5-9B-4bit")
