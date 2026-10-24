from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from omm import cli, config, rag_adapter, rag_connect, rag_mcp
from omm.documents import DocumentStore

ORIGINAL = "실험 안내\n자기장 측정 실험의 준비물은 나침반과 코일입니다.\n안전 수칙을 지켜 주세요."


def _messages(*requests):
    return b"".join(json.dumps(r, ensure_ascii=False).encode() + b"\n" for r in requests)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call(identifier, name, arguments):
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_stdio_round_trip_over_real_pipes(isolated_omm_home):
    DocumentStore().import_document("전자기학.md", ORIGINAL.encode())
    env = {**os.environ, "OMM_HOME": str(config.OMM_HOME),
           "PYTHONPATH": os.pathsep.join([str(Path(rag_mcp.__file__).parents[1]), os.environ.get("PYTHONPATH", "")])}
    process = subprocess.run(
        [sys.executable, "-m", "omm.rag_mcp"],
        input=_messages(INIT, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        _call(3, "search_documents", {"query": "자기장 측정 준비물은?"}),
                        _call(4, "search_documents", {"query": "dinosaurs asteroid extinction"}),
                        {"jsonrpc": "2.0", "id": 5, "method": "ping"}),
        capture_output=True, env=env, timeout=60,
    )
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5]  # The notification gets no reply.
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"search_documents", "list_collections"}
    found = replies[2]["result"]
    assert found["isError"] is False and found["structuredContent"]["found"] is True
    hit = found["structuredContent"]["results"][0]
    assert hit["document"] == "전자기학.md" and hit["page"] == 1
    assert hit["quote"] == ORIGINAL[hit["start"]:hit["end"]] and "나침반" in hit["quote"]
    assert hit["method"] == "lexical"  # conftest replaces the embedder, so search falls back to words only
    assert "전자기학.md p.1" in found["content"][0]["text"]
    missing = replies[3]["result"]
    assert missing["structuredContent"]["found"] is False and missing["structuredContent"]["results"] == []
    assert missing["content"][0]["text"].startswith("근거 없음")
    assert replies[4]["result"] == {}
    # Questions and document text never reach diagnostics.
    stderr = process.stderr.decode("utf-8", "replace")
    assert "자기장" not in stderr and "나침반" not in stderr and "dinosaurs" not in stderr


def test_no_documents_is_explicit_not_empty_success(isolated_omm_home):
    server = rag_mcp.Server()
    server.handle_line(json.dumps(INIT))
    reply = json.loads(server.handle_line(json.dumps(_call(2, "search_documents", {"query": "anything here"}))))
    assert reply["result"]["structuredContent"]["found"] is False
    assert "등록된 문서가 없어요" in reply["result"]["content"][0]["text"]
    listed = json.loads(server.handle_line(json.dumps(_call(3, "list_collections", {}))))["result"]
    assert listed["structuredContent"]["collections_supported"] is True  # collections exist since the hybrid store landed


def test_protocol_errors_and_tool_errors(isolated_omm_home):
    server = rag_mcp.Server()
    assert json.loads(server.handle_line(b"{not json"))["error"]["code"] == rag_mcp.PARSE_ERROR
    assert json.loads(server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})))["error"]["code"] == rag_mcp.METHOD_NOT_FOUND
    assert json.loads(server.handle_line(json.dumps(_call(2, "nope", {}))))["error"]["code"] == rag_mcp.INVALID_PARAMS
    bad = json.loads(server.handle_line(json.dumps(_call(3, "search_documents", {"query": ""}))))["result"]
    assert bad["isError"] is True
    with_collection = json.loads(server.handle_line(json.dumps(_call(4, "search_documents", {"query": "x y", "collection": "인사규정"}))))["result"]
    assert with_collection["isError"] is True and "list_collections" in with_collection["content"][0]["text"]
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}})) is None
    # Older clients get their version back and no structuredContent.
    old = json.loads(server.handle_line(json.dumps({**INIT, "params": {"protocolVersion": "2024-11-05"}})))
    assert old["result"]["protocolVersion"] == "2024-11-05"
    unknown = json.loads(server.handle_line(json.dumps({**INIT, "params": {"protocolVersion": "1999-01-01"}})))
    assert unknown["result"]["protocolVersion"] == rag_mcp.PROTOCOL_VERSIONS[0]


def test_adapter_passes_collection_through_when_store_supports_it():
    calls = []

    class FutureStore:
        def list(self):
            return [{"id": "a", "name": "규정.pdf", "collection": "인사규정", "index_current": True},
                    {"id": "b", "name": "영업.pdf", "collection": "영업자료", "index_current": True}]

        def collections(self):
            return [{"name": "인사규정"}, {"name": "영업자료"}]

        def search(self, question, ids, collection=None):
            calls.append((ids, collection))
            return 1, [{"name": "규정.pdf", "page": 2, "text": "휴가는 15일", "start": 0, "end": 7,
                        "sha256": "x", "score": 0.9, "method": "hybrid_v1", "collection": collection}]

    result = rag_adapter.search("휴가 일수", "인사규정", store=FutureStore())
    assert calls == [(["a"], "인사규정")]
    assert result["found"] and result["results"][0]["method"] == "hybrid_v1" and result["results"][0]["score"] == 0.9
    assert rag_adapter.list_collections(store=FutureStore())["collections_supported"] is True


def test_adapter_batches_more_than_twenty_documents():
    class ManyStore:
        def list(self):
            return [{"id": str(i), "name": f"d{i}.txt", "index_current": i != 0} for i in range(45)]

        def search(self, question, ids):
            assert 1 <= len(ids) <= 20
            return 1, [{"name": f"d{ids[0]}.txt", "page": 1, "text": "t", "start": 0, "end": 1}]

    result = rag_adapter.search("hello world", limit=10, store=ManyStore())
    assert result["searched_documents"] == 44 and result["skipped_stale"] == ["d0.txt"]
    assert [r["document"] for r in result["results"]] == ["d1.txt", "d21.txt", "d41.txt"]


# ---------------------------------------------------------------- connect --


@pytest.fixture
def home(tmp_path, monkeypatch, isolated_omm_home):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rag_connect.shutil, "which", lambda name: "/installed/" + name)
    return root


SEEDS = {
    "claude-code": b'{\n  "numStartups": 3,\n  "mcpServers": {\n    "other": {"type": "stdio", "command": "x", "args": []}\n  },\n  "projects": {}\n}\n',
    "codex": b'# keep this comment\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n',
    "pi": b'{ "mcpServers": {} }\n',
    "cursor": b'{"mcpServers": {"other": {"command": "x"}}}',
}


def _entry(harness, path):
    raw = path.read_bytes()
    if harness == "codex":
        import tomllib
        return tomllib.loads(raw.decode()).get("mcp_servers", {}).get(rag_connect.SERVER_NAME)
    return json.loads(raw).get("mcpServers", {}).get(rag_connect.SERVER_NAME)


@pytest.mark.parametrize("harness", rag_connect.HARNESSES)
def test_connect_is_idempotent_and_disconnect_restores_exact_bytes(harness, home):
    path = rag_connect.config_path(harness)
    assert home in path.parents
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SEEDS[harness])
    assert rag_connect.connect(harness)["state"] == "added"
    entry = _entry(harness, path)
    assert entry["command"] == os.path.abspath(sys.executable)
    assert entry["args"] == ["-m", "omm.rag_mcp"]
    assert entry["env"] == {"OMM_HOME": str(config.OMM_HOME)}
    assert rag_connect.connect(harness)["state"] == "unchanged"
    assert rag_connect.status(harness)["current_command"] is True
    assert rag_connect.disconnect(harness)["state"] == "restored"
    assert path.read_bytes() == SEEDS[harness]
    assert rag_connect.disconnect(harness)["state"] == "not_connected"


@pytest.mark.parametrize("harness", rag_connect.HARNESSES)
def test_disconnect_keeps_later_user_edits(harness, home):
    path = rag_connect.config_path(harness)
    rag_connect.connect(harness)  # No file yet: created from nothing.
    if harness == "codex":
        path.write_bytes(path.read_bytes() + b'\n[profiles.fast]\nmodel = "o4"\n')
    else:
        data = json.loads(path.read_bytes())
        data["userEdit"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
    assert rag_connect.disconnect(harness)["state"] == "removed"
    assert _entry(harness, path) is None
    text = path.read_text(encoding="utf-8")
    assert ("profiles.fast" in text) if harness == "codex" else json.loads(text) == {"userEdit": True}


def test_codex_toml_keeps_comments_and_other_servers(home):
    path = rag_connect.config_path("codex")
    path.parent.mkdir(parents=True)
    path.write_bytes(SEEDS["codex"])
    rag_connect.connect("codex")
    text = path.read_text(encoding="utf-8")
    assert "# keep this comment" in text and "[mcp_servers.other]" in text
    assert "[mcp_servers.omm-documents]" in text and "startup_timeout_sec = 30" in text
    path.write_text(text.replace('model = "gpt-5"', 'model = "gpt-5.5"'), encoding="utf-8")
    assert rag_connect.disconnect("codex")["state"] == "removed"
    assert path.read_text(encoding="utf-8") == SEEDS["codex"].decode().replace('"gpt-5"', '"gpt-5.5"')


def test_refuses_foreign_entry_and_edited_entry(home):
    path = rag_connect.config_path("cursor")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {rag_connect.SERVER_NAME: {"command": "mine"}}}), encoding="utf-8")
    with pytest.raises(rag_connect.RagConnectionError, match="did not add"):
        rag_connect.connect("cursor")
    path.unlink()
    rag_connect.connect("cursor")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"][rag_connect.SERVER_NAME]["args"].append("--edited")
    data["other"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(rag_connect.RagConnectionError, match="edited"):
        rag_connect.disconnect("cursor")
    assert rag_connect.SERVER_NAME in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_not_installed_is_clear(home, monkeypatch):
    monkeypatch.setattr(rag_connect.shutil, "which", lambda name: None)
    with pytest.raises(rag_connect.RagConnectionError, match="was not found"):
        rag_connect.connect("codex")
    assert not rag_connect.config_path("codex").exists()


def test_env_overrides_and_frozen_command(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-dir"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-dir"))
    assert rag_connect.config_path("codex") == tmp_path / "codex-home" / "config.toml"
    assert rag_connect.config_path("claude-code") == tmp_path / "claude-dir" / ".claude.json"
    assert rag_connect.config_path("pi") == tmp_path / "pi-dir" / "mcp.json"
    monkeypatch.setattr(rag_connect.sys, "frozen", True, raising=False)
    assert rag_connect.server_command()[1:] == ["rag", "mcp"]


def test_cli_connect_and_disconnect(home):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["rag", "connect", "pi"])
    assert result.exit_code == 0, result.output
    assert "omm-documents" in result.output
    assert runner.invoke(cli.app, ["rag", "connect", "vim"]).exit_code == 1
    result = runner.invoke(cli.app, ["rag", "disconnect", "pi"])
    assert result.exit_code == 0 and "restored" in result.output.lower()
    assert not rag_connect.config_path("pi").exists()


def test_cli_rag_mcp_serves_stdio(isolated_omm_home):
    # The frozen (npm / portable) registration starts `omm rag mcp`.
    result = CliRunner().invoke(cli.app, ["rag", "mcp"], input=_messages(INIT, {"jsonrpc": "2.0", "id": 2, "method": "ping"}))
    assert result.exit_code == 0, result.output
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [r["id"] for r in replies] == [1, 2]


def test_adapter_reports_hybrid_or_lexical_method_from_detailed_search():
    class DetailedStore:
        def list(self):
            return [{"id": "a", "name": "규정.pdf", "collection": "인사규정", "index_current": True}]

        def collections(self):
            return [{"name": "인사규정"}]

        def search(self, question, ids, collection=None):
            raise AssertionError("search_detailed must be preferred")

        def search_detailed(self, question, ids=None, *, collection=None, limit=3):
            row = {"document_id": "a", "name": "규정.pdf", "page": 1, "start": 0, "end": 4, "text": "연차 15일",
                   "sha256": "x", "score": 0.03, "method": self.method, "collection": "인사규정"}
            return {"revision": 1, "sources": [row], "method": self.method, "embedding_model": "bge-m3",
                    "notice": None if self.method == "hybrid" else "Ollama에 연결하지 못해 단어 검색만 했어요."}

    store = DetailedStore()
    store.method = "hybrid"
    result = rag_adapter.search("쉬는 날 며칠?", store=store)
    assert result["found"] is True and result["method"] == "hybrid" and result["notice"] is None
    store.method = "lexical"
    result = rag_adapter.search("연차", store=store)
    assert result["method"] == "lexical" and "단어 검색만" in result["notice"]
