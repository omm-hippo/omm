from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omm import agent_connection as agent, cli


@pytest.fixture
def home(tmp_path, monkeypatch, isolated_omm_home):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/installed/" + name)
    return root


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(agent, "preflight", lambda model, base: {
        "model": model, "base_url": base, "context_length": 65536,
        "weights_bytes": 0, "kv_bytes_per_token": None,
        "tools_advertised": True, "tool_workflow_verified": False,
    })
    calls = []
    monkeypatch.setattr(agent, "_create_runtime_model", lambda *args: calls.append(("create", *args)))
    monkeypatch.setattr(agent, "_delete_runtime_model", lambda metadata: calls.append(("delete", metadata["runtime_model"])))
    return calls


def seed(harness):
    paths = agent._paths(harness)
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (b'# preserve this comment\nmodel = "cloud"\nmodel_provider = "openai"\n\n[projects."/work"]\ntrust_level = "trusted"\n'
               if path.suffix == ".toml" else
               b'{ "theme": "light", "private": "secret-token", "env": {"UNRELATED": "keep"} }\n')
        path.write_bytes(raw)
    return paths


@pytest.mark.parametrize("harness", agent.HARNESSES)
def test_exact_original_bytes_restored(harness, home, available):
    paths = seed(harness)
    original = [p.read_bytes() for p in paths]
    agent.connect(harness, "local-model")
    assert agent.status(harness)["state"] == "local"
    for path in paths:
        assert "secret-token" not in agent.status(harness).values()
        assert path.read_bytes() != original[paths.index(path)]
    assert agent.restore(harness)
    assert [p.read_bytes() for p in paths] == original
    assert agent.status(harness)["state"] == "unmanaged"
    assert not agent.restore(harness)


@pytest.mark.parametrize("harness", agent.HARNESSES)
def test_unrelated_edits_survive_restore(harness, home, available):
    paths = seed(harness)
    agent.connect(harness, "local-model")
    for path in paths:
        document = agent._parse(path.read_bytes(), path.suffix == ".toml")
        document["theme"] = "dark"
        path.write_bytes(agent._dump(document, path.suffix == ".toml"))
    assert agent.restore(harness)
    for path in paths:
        data = agent._parse(path.read_bytes(), path.suffix == ".toml")
        assert data["theme"] == "dark"
        if path.suffix == ".toml":
            assert data["model"] == "cloud"
            assert data["model_provider"] == "openai"
            assert "preserve this comment" in path.read_text(encoding="utf-8")
            assert "model_providers" not in data
        else:
            assert data["private"] == "secret-token"
            assert data["env"] == {"UNRELATED": "keep"}
            assert "providers" not in data
            assert "defaultModel" not in data


@pytest.mark.parametrize("harness", agent.HARNESSES)
def test_new_files_removed_and_new_unrelated_fields_kept(harness, home, available):
    paths = agent._paths(harness)
    agent.connect(harness, "local-model")
    assert all(p.exists() for p in paths)
    agent.restore(harness)
    assert not any(p.exists() for p in paths)
    agent.connect(harness, "local-model")
    path = paths[0]
    document = agent._parse(path.read_bytes(), path.suffix == ".toml")
    document["theme"] = "dark"
    path.write_bytes(agent._dump(document, path.suffix == ".toml"))
    agent.restore(harness)
    assert agent._parse(path.read_bytes(), path.suffix == ".toml") == {"theme": "dark"}


@pytest.mark.parametrize("harness", agent.HARNESSES)
def test_conflicting_routing_edits_are_never_overwritten(harness, home, available):
    paths = seed(harness)
    agent.connect(harness, "local-model")
    path = paths[-1]
    is_toml = path.suffix == ".toml"
    data = agent._parse(path.read_bytes(), is_toml)
    data["defaultModel" if harness == "pi" else "model"] = "manual-selection"
    path.write_bytes(agent._dump(data, is_toml))
    before = [p.read_bytes() for p in paths]
    with pytest.raises(agent.ConnectionError, match="routing field was edited"):
        agent.restore(harness)
    assert [p.read_bytes() for p in paths] == before
    assert agent._journal(harness).exists()


def test_multi_file_write_interruption_is_recoverable(home, available, monkeypatch):
    paths = seed("pi")
    before = [p.read_bytes() for p in paths]
    write = agent._write
    def fail_second(path, raw):
        if path == paths[1]:
            raise OSError("disk unavailable")
        write(path, raw)
    monkeypatch.setattr(agent, "_write", fail_second)
    with pytest.raises(agent.ConnectionError, match="interrupted"):
        agent.connect("pi", "local-model")
    assert paths[0].read_bytes() != before[0]
    assert agent.status("pi")["state"] == "changed_or_interrupted"
    monkeypatch.setattr(agent, "_write", write)
    agent.restore("pi")
    assert [p.read_bytes() for p in paths] == before


def test_restore_interruption_keeps_original_backup(home, available, monkeypatch):
    paths = seed("pi")
    before = [p.read_bytes() for p in paths]
    agent.connect("pi", "local-model")
    write = agent._write
    def fail_second(path, raw):
        if path == paths[1]:
            raise OSError("disk unavailable")
        write(path, raw)
    monkeypatch.setattr(agent, "_write", fail_second)
    with pytest.raises(agent.ConnectionError, match="interrupted"):
        agent.restore("pi")
    assert agent._journal("pi").exists()
    monkeypatch.setattr(agent, "_write", write)
    agent.restore("pi")
    assert [p.read_bytes() for p in paths] == before


def test_duplicate_connection_never_replaces_backup(home, available):
    seed("codex")
    agent.connect("codex", "one")
    original = agent._journal("codex").read_bytes()
    with pytest.raises(agent.ConnectionError, match="already exists"):
        agent.connect("codex", "two")
    assert agent._journal("codex").read_bytes() == original


def test_invalid_journal_is_preserved_with_actionable_error(home, available):
    agent.connect("claude", "model")
    journal = agent._journal("claude")
    contents = json.loads(journal.read_bytes())
    contents.pop("phase")
    journal.write_text(json.dumps(contents), encoding="utf-8")
    before = agent._paths("claude")[0].read_bytes()
    with pytest.raises(agent.ConnectionError, match="backup is invalid"):
        agent.status("claude")
    with pytest.raises(agent.ConnectionError, match="backup is invalid"):
        agent.restore("claude")
    assert journal.exists() and agent._paths("claude")[0].read_bytes() == before


@pytest.mark.parametrize("contents", [b'[]', b'{"env":false}', b'{"model":false}', b'{"model":1,"model":2}', b'{broken', b'\xff'])
def test_invalid_json_never_changes_configuration(home, available, contents):
    [path] = agent._paths("claude")
    path.parent.mkdir()
    path.write_bytes(contents)
    with pytest.raises(agent.ConnectionError):
        agent.connect("claude", "local-model")
    assert path.read_bytes() == contents
    assert not agent._journal("claude").exists()


def test_invalid_toml_never_changes_configuration(home, available):
    [path] = seed("codex")
    path.write_text('model = ["bad"', encoding="utf-8")
    with pytest.raises(agent.ConnectionError):
        agent.connect("codex", "local-model")
    assert path.read_text(encoding="utf-8") == 'model = ["bad"'


def test_missing_harness_refuses_before_backup(home, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    with pytest.raises(agent.ConnectionError, match="not installed"):
        agent.connect("pi", "model")
    assert not agent._journal("pi").exists()


def test_symlink_directory_refused(home, available, tmp_path, requires_symlink_support):
    target = tmp_path / "actual"
    target.mkdir()
    (home / ".claude").symlink_to(target, target_is_directory=True)
    with pytest.raises(agent.ConnectionError, match="symbolic"):
        agent.connect("claude", "local-model")
    assert list(target.iterdir()) == []


def test_directory_override_and_moved_backup(home, available, monkeypatch, tmp_path):
    root = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(root))
    agent.connect("codex", "local-model")
    assert (root / "config.toml").exists()
    assert not (home / ".codex").exists()
    monkeypatch.delenv("CODEX_HOME")
    with pytest.raises(agent.ConnectionError, match="directory changed"):
        agent.restore("codex")
    monkeypatch.setenv("CODEX_HOME", str(root))
    agent.restore("codex")
    assert not (root / "config.toml").exists()


@pytest.fixture
def ollama():
    state = {"version": "0.19.0", "capabilities": ["completion", "tools"],
             "model_info": {"qwen.context_length": 65536}, "models": [], "requests": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            state["requests"].append(("GET", self.path, None))
            self.reply({"version": state["version"]} if self.path == "/api/version" else {"models": state["models"]})
        def do_DELETE(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("DELETE", self.path, body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, body))
            self.reply({"status": "success"} if self.path == "/api/create" else
                       {k: v for k, v in state.items() if k not in ("requests", "models")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def test_cli_local_http_to_persisted_settings_and_exact_restore(home, ollama, monkeypatch):
    url, state = ollama
    monkeypatch.setattr(agent, "_total_ram_bytes", lambda: 8 * 2**30)
    [path] = seed("codex")
    original = path.read_bytes()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["agent", "local", "codex", "--model", "local-model", "--base-url", url])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "not verified" in output
    assert "local-model:latest-omm16k with a 16K-token context" in output
    document = agent._parse(path.read_bytes(), True)
    assert document["model_providers"]["omm-local"]["base_url"] == url + "/v1"
    assert document["model"] == "local-model:latest-omm16k"
    assert state["requests"] == [
        ("GET", "/api/version", None), ("POST", "/api/show", {"model": "local-model"}), ("GET", "/api/tags", None),
        ("POST", "/api/create", {"model": "local-model:latest-omm16k", "from": "local-model",
                                 "parameters": {"num_ctx": 16384}, "stream": False})]
    result = runner.invoke(cli.app, ["agent", "cloud", "codex"])
    assert result.exit_code == 0, result.output
    assert "ollama rm" not in result.output
    assert path.read_bytes() == original
    assert state["requests"][-1] == ("DELETE", "/api/delete", {"model": "local-model:latest-omm16k"})


def test_cloud_names_the_leftover_model_when_ollama_is_off(home, ollama, monkeypatch):
    url, state = ollama
    agent.connect("pi", "local-model", base_url=url)
    monkeypatch.setattr(agent.LoopbackJsonClient, "request", lambda *a, **k: (_ for _ in ()).throw(
        agent.RuntimeAdapterError("server_unavailable", "down")))
    result = CliRunner().invoke(cli.app, ["agent", "cloud", "pi"])
    assert result.exit_code == 0, result.output
    assert "ollama rm local-model:latest-omm16k" in " ".join(result.output.split())
    assert not agent._journal("pi").exists()


def test_failed_model_creation_writes_nothing(home, ollama, monkeypatch):
    url, state = ollama
    paths = seed("pi")
    before = [p.read_bytes() for p in paths]
    def refuse(*args):
        raise agent.ConnectionError("Ollama could not create the runtime-context model; no settings changed.")
    monkeypatch.setattr(agent, "_create_runtime_model", refuse)
    with pytest.raises(agent.ConnectionError, match="runtime-context"):
        agent.connect("pi", "local-model", base_url=url)
    assert [p.read_bytes() for p in paths] == before
    assert not agent._journal("pi").exists()


def test_pi_window_matches_the_runtime_context(home, available):
    agent.connect("pi", "qwen3:0.6b")
    models = agent._parse(agent._paths("pi")[0].read_bytes())
    [entry] = models["providers"]["omm-local"]["models"]
    assert entry["id"] == "qwen3:0.6b-omm16k"
    assert entry["contextWindow"] == 16384
    assert available == [("create", "http://127.0.0.1:11434", "qwen3:0.6b", "qwen3:0.6b-omm16k", 16384)]
    agent.restore("pi")
    assert available[-1] == ("delete", "qwen3:0.6b-omm16k")


QWEN3_06B = {"general.architecture": "qwen3", "qwen3.block_count": 28, "qwen3.attention.head_count": 16,
             "qwen3.attention.head_count_kv": 8, "qwen3.attention.key_length": 128,
             "qwen3.attention.value_length": 128, "qwen3.embedding_length": 1024}


def test_kv_estimate_from_gguf_metadata():
    assert agent._kv_bytes_per_token(QWEN3_06B) == 28 * 8 * 256 * 2
    llama = {"general.architecture": "llama", "llama.block_count": 32, "llama.attention.head_count": 32,
             "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096}
    assert agent._kv_bytes_per_token(llama) == 32 * 8 * 256 * 2
    per_layer = {**QWEN3_06B, "qwen3.attention.head_count_kv": [8, 0, 8]}
    assert agent._kv_bytes_per_token(per_layer) == 16 * 256 * 2
    assert agent._kv_bytes_per_token({"general.architecture": "x"}) is None
    assert agent._kv_bytes_per_token(None) is None


@pytest.mark.parametrize("ram_gib, weights, model_context, expected", [
    (8, 435 * 2**20, 40960, 16384),     # qwen3-0.6b on an 8 GB Mac: 32K would need 3.7 GB of KV
    (16, 435 * 2**20, 40960, 32768),    # capped by the model's own context
    (64, 435 * 2**20, 131072, 65536),
    (24, 5 * 2**30, 131072, 32768),
])
def test_runtime_context_fits_the_memory_share(ram_gib, weights, model_context, expected):
    metadata = {"context_length": model_context, "weights_bytes": weights,
                "kv_bytes_per_token": agent._kv_bytes_per_token(QWEN3_06B)}
    assert agent.runtime_context(metadata, ram_gib * 2**30) == expected


def test_runtime_context_refuses_a_model_too_big_for_memory():
    # An 8B model on an 8 GB Mac nearly froze it during a live test.
    metadata = {"context_length": 131072, "weights_bytes": 4_900_000_000, "kv_bytes_per_token": 131072}
    with pytest.raises(agent.ConnectionError, match="smaller model"):
        agent.runtime_context(metadata, 8 * 2**30)


def test_runtime_context_without_layer_metadata_takes_the_smallest():
    assert agent.runtime_context({"context_length": 65536, "kv_bytes_per_token": None}, 64 * 2**30) == 16384


@pytest.mark.parametrize("model, derived", [
    ("qwen3:8b", "qwen3:8b-omm32k"),
    ("llama3.2", "llama3.2:latest-omm32k"),
    ("hf.co/user/repo:Q4_K_M", "hf.co/user/repo:Q4_K_M-omm32k"),
    ("localhost:5000/repo", "localhost:5000/repo:latest-omm32k"),
])
def test_runtime_model_tag(model, derived):
    assert agent.runtime_model(model, 32768) == derived


def test_picker_hides_derived_models(monkeypatch):
    class Client:
        def __init__(self, base):
            pass
        def request(self, *args, **kwargs):
            return SimpleNamespace(data={"models": [{"name": "qwen3:8b"}, {"name": "qwen3:8b-omm32k"}]})
    monkeypatch.setattr(agent, "LoopbackJsonClient", Client)
    assert agent.installed_models() == ["qwen3:8b"]


@pytest.mark.parametrize("update, message", [
    ({"version": "0.13.9"}, "0.14.0"),
    ({"capabilities": ["completion"]}, "tool calling"),
    ({"model_info": {"a.context_length": 8192}}, "32768"),
    ({"remote_host": "https://cloud.example"}, "remote service"),
])
def test_preflight_rejections_do_not_write(home, ollama, update, message):
    url, state = ollama
    state.update(update)
    with pytest.raises(agent.ConnectionError, match=message):
        agent.connect("claude", "local-model", base_url=url)
    assert not agent._paths("claude")[0].exists()
    assert not agent._journal("claude").exists()


@pytest.mark.parametrize("model", ["name:cloud", "name-cloud", "!shell", "bad\nmodel"])
def test_cloud_or_invalid_tags_refused_before_requests(home, ollama, model):
    url, state = ollama
    with pytest.raises(agent.ConnectionError):
        agent.preflight(model, url)
    assert state["requests"] == []


def test_non_loopback_refused(home):
    with pytest.raises(agent.ConnectionError, match="loopback"):
        agent.preflight("model", "http://example.com")


def test_private_backup_permissions(home, available):
    agent.connect("claude", "model")
    if __import__("os").name != "nt":
        assert agent._journal("claude").stat().st_mode & 0o777 == 0o600
        assert agent._journal("claude").parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX directory permissions")
def test_existing_backup_directory_is_private_before_lock(home, available, monkeypatch):
    directory = agent._journal("claude").parent
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    original_locked = agent.atomic.locked

    def locked(path):
        assert directory.stat().st_mode & 0o777 == 0o700
        return original_locked(path)

    monkeypatch.setattr(agent.atomic, "locked", locked)
    agent.connect("claude", "model")
    directory.chmod(0o755)
    assert agent.restore("claude")
    assert directory.stat().st_mode & 0o777 == 0o700


def test_original_file_permissions_restored(home, available):
    [path] = seed("claude")
    path.chmod(0o640)
    original = path.stat().st_mode & 0o777
    agent.connect("claude", "model")
    agent.restore("claude")
    assert path.stat().st_mode & 0o777 == original


def test_status_does_not_print_existing_secrets(home, available):
    seed("claude")
    agent.connect("claude", "model")
    result = CliRunner().invoke(cli.app, ["agent", "status", "claude"])
    assert result.exit_code == 0
    assert "secret-token" not in result.output


def test_private_backup_is_excluded_from_support_and_usage_payloads(home, available, monkeypatch):
    from omm import support_report, usage
    seed("claude")
    agent.connect("claude", "model")
    monkeypatch.setattr(usage, "_snapshot", lambda **kwargs: {})
    report = support_report.build(SimpleNamespace(status="PASS", checks=[]), include=["policies", "stages"])
    assert "secret-token" not in support_report.preview(report)
    assert "secret-token" not in json.dumps(usage.build_payload([]))
