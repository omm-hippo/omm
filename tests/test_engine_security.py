from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest
from typer.testing import CliRunner

from omm import cli, engine_security


def _connection(address, *, pid=42, port=11434):
    return SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip=address, port=port),
        pid=pid,
    )


def test_listener_scope_distinguishes_loopback_and_external(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security, "_expected_process", lambda engine, pid: True)
    monkeypatch.setattr(engine_security, "_owned_ollama_process", lambda pid: None)

    monkeypatch.setattr(engine_security.psutil, "net_connections", lambda kind: [_connection("127.0.0.1")])
    assert engine_security.inspect("ollama")["status"] == "local_only"

    monkeypatch.setattr(engine_security.psutil, "net_connections", lambda kind: [_connection("0.0.0.0")])
    result = engine_security.inspect("ollama")
    assert result["status"] == "external_allowed"
    assert result["owned_by_omm"] is False


def test_unidentified_listener_is_unknown_not_safe(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security, "_expected_process", lambda engine, pid: False)
    monkeypatch.setattr(engine_security.psutil, "net_connections", lambda kind: [_connection("127.0.0.1")])
    assert engine_security.inspect("ollama")["status"] == "unknown"


def test_fix_never_restarts_external_or_lmstudio_owned_server(monkeypatch):
    monkeypatch.setattr(
        engine_security,
        "inspect",
        lambda engine: {
            "engine": engine,
            "status": "external_allowed",
            "owned_by_omm": False,
            "pid": 42,
        },
    )
    with pytest.raises(engine_security.EngineSecurityError, match="left unchanged"):
        engine_security.fix_local_only("ollama")
    with pytest.raises(engine_security.EngineSecurityError, match="engine itself"):
        engine_security.fix_local_only("lmstudio")


def test_cli_fix_previews_disconnect_and_restart_impact(isolated_omm_home, monkeypatch):
    initial = {
        "engine": "ollama",
        "status": "external_allowed",
        "reason": "wildcard listener",
        "port": 11434,
        "listeners": [{"address": "0.0.0.0", "port": 11434}],
        "owned_by_omm": True,
        "pid": 42,
    }
    final = {
        **initial,
        "status": "local_only",
        "reason": "loopback only",
        "listeners": [{"address": "127.0.0.1", "port": 11434}],
        "changed": True,
    }
    monkeypatch.setattr(engine_security, "inspect", lambda engine: initial)
    monkeypatch.setattr(engine_security, "fix_local_only", lambda engine: final)
    result = CliRunner().invoke(
        cli.app, ["engine", "security", "ollama", "--fix-local-only", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "clients will disconnect" in result.output
    assert "Restart Ollama on 127.0.0.1" in result.output
