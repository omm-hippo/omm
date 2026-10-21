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


def test_nothing_listening_is_not_running_not_unknown(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security.psutil, "net_connections", lambda kind: [])
    result = engine_security.inspect("ollama")
    assert result["status"] == "not_running"
    assert "11434" in result["reason"]


def test_unreadable_listener_table_stays_unknown(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)

    def unreadable(kind):
        raise OSError("socket table unavailable")

    monkeypatch.setattr(engine_security.psutil, "net_connections", unreadable)
    assert engine_security.inspect("ollama")["status"] == "unknown"


def test_lmstudio_reported_stopped_is_not_running(monkeypatch):
    monkeypatch.setattr(engine_security.linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(
        engine_security.linker, "_lmstudio_server_status", lambda path: {"running": False}
    )
    assert engine_security.inspect("lmstudio")["status"] == "not_running"


def test_lmstudio_without_lms_or_status_is_unknown(monkeypatch):
    monkeypatch.setattr(engine_security.linker, "_lms_cli_path", lambda: None)
    assert engine_security.inspect("lmstudio")["status"] == "unknown"
    monkeypatch.setattr(engine_security.linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(engine_security.linker, "_lmstudio_server_status", lambda path: None)
    assert engine_security.inspect("lmstudio")["status"] == "unknown"


def test_fix_on_stopped_server_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        engine_security,
        "inspect",
        lambda engine: {"engine": engine, "status": "not_running", "owned_by_omm": False},
    )
    assert engine_security.fix_local_only("ollama")["changed"] is False


def test_cli_labels_stopped_server_plainly(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        engine_security,
        "inspect",
        lambda engine: {
            "engine": engine,
            "status": "not_running",
            "reason": "nothing is listening on port 11434, so no device can connect to it",
            "listeners": [],
            "owned_by_omm": False,
        },
    )
    result = CliRunner().invoke(cli.app, ["engine", "security", "ollama"])
    assert result.exit_code == 0, result.output
    assert "Not running" in result.output
    assert "Could not determine" not in result.output


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


def _process(pid, connections=(), *, denied=False):
    def net_connections(kind):
        if denied:
            raise psutil.AccessDenied(pid)
        return list(connections)

    return SimpleNamespace(pid=pid, net_connections=net_connections)


def _deny_system_table(kind):
    raise psutil.AccessDenied()


def test_macos_denied_system_table_falls_back_to_per_process_listeners(monkeypatch):
    # macOS refuses psutil.net_connections() without root even for the user's own engine.
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security, "_expected_process", lambda engine, pid: pid == 42)
    monkeypatch.setattr(engine_security, "_owned_ollama_process", lambda pid: None)
    monkeypatch.setattr(engine_security.psutil, "net_connections", _deny_system_table)
    listener = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=11434))
    monkeypatch.setattr(
        engine_security.psutil,
        "process_iter",
        lambda: [_process(1, denied=True), _process(42, [listener])],
    )

    result = engine_security.inspect("ollama")

    # Listener scope does not depend on ownership; a user-launched Ollama is still local-only.
    assert result["status"] == "local_only"
    assert result["owned_by_omm"] is False
    assert result["pid"] == 42


def test_stopped_server_reports_not_running_instead_of_access_denied(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security.psutil, "net_connections", _deny_system_table)
    monkeypatch.setattr(engine_security.psutil, "process_iter", lambda: [_process(1, denied=True)])
    monkeypatch.setattr(engine_security, "_port_accepts_connections", lambda port: False)

    result = engine_security.inspect("ollama")

    assert result["status"] == "not_running"
    assert "not running" in result["reason"]
    assert "AccessDenied" not in result["reason"]


def test_listener_in_uninspectable_process_stays_unknown_with_reason(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security.psutil, "net_connections", _deny_system_table)
    monkeypatch.setattr(engine_security.psutil, "process_iter", lambda: [_process(1, denied=True)])
    monkeypatch.setattr(engine_security, "_port_accepts_connections", lambda port: True)

    result = engine_security.inspect("ollama")

    assert result["status"] == "unknown"
    assert "cannot inspect" in result["reason"]


def test_empty_system_table_reports_not_running(monkeypatch):
    monkeypatch.setattr(engine_security, "_port", lambda engine: 11434)
    monkeypatch.setattr(engine_security.psutil, "net_connections", lambda kind: [])

    assert "not running" in engine_security.inspect("ollama")["reason"]
