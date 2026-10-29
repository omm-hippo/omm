from __future__ import annotations

from pathlib import Path

import pytest
import questionary
from typer.testing import CliRunner

from omm import agent_connection, agent_launch, cli, config

META = {"model": "qwen3:8b", "base_url": "http://127.0.0.1:11434", "context_length": 65536,
        "tools_advertised": True, "tool_workflow_verified": False}


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(agent_launch.shutil, "which", lambda name: "/installed/" + name)


def _overrides(argv):
    return [argv[i + 1] for i, token in enumerate(argv) if token == "-c"]


def test_codex_routes_through_command_line_overrides_only(installed):
    argv, env = agent_launch.command("codex", META, ["exec", "hi"])
    assert argv[0] == "/installed/codex" and argv[-2:] == ["exec", "hi"]
    assert env == {}
    assert _overrides(argv) == [
        'model_provider="omm-local"',
        'model_providers.omm-local={name = "OMM local Ollama", base_url = "http://127.0.0.1:11434/v1", '
        'wire_api = "responses", requires_openai_auth = false}',
        'model="qwen3:8b"',
        "model_context_window=65536",
        "model_auto_compact_token_limit=58982",
        "features.multi_agent=false",
        'web_search="disabled"',
    ]


def test_codex_provider_override_is_valid_toml(installed):
    import tomlkit

    argv, _ = agent_launch.command("codex", META)
    provider = next(o for o in _overrides(argv) if o.startswith("model_providers."))
    table = tomlkit.parse("x = " + provider.split("=", 1)[1])["x"]
    assert dict(table) == agent_connection.codex_provider(META["base_url"])


def test_claude_routes_through_process_environment(installed):
    argv, env = agent_launch.command("claude", META, ["-p", "hi"])
    assert argv == ["/installed/claude", "--model", "qwen3:8b", "-p", "hi"]
    assert env == agent_connection.claude_env("qwen3:8b", META["base_url"])


def test_unsupported_or_missing_harness_is_refused(monkeypatch):
    with pytest.raises(agent_launch.LaunchError, match="setting agent local pi"):
        agent_launch.command("pi", META)
    monkeypatch.setattr(agent_launch.shutil, "which", lambda name: None)
    with pytest.raises(agent_launch.LaunchError, match="not installed"):
        agent_launch.command("codex", META)


def test_run_waits_through_ctrl_c_and_returns_child_status(monkeypatch):
    seen = {}

    class Child:
        interrupts = 1

        def __init__(self, argv, env):
            seen["argv"], seen["env"] = argv, env

        def wait(self):
            if Child.interrupts:
                Child.interrupts -= 1
                raise KeyboardInterrupt
            return 3

    monkeypatch.setattr(agent_launch.subprocess, "Popen", Child)
    monkeypatch.setenv("KEEP_ME", "1")
    assert agent_launch.run(["codex"], {"ANTHROPIC_API_KEY": ""}) == 3
    assert seen["env"]["KEEP_ME"] == "1" and seen["env"]["ANTHROPIC_API_KEY"] == ""


@pytest.fixture
def launched(isolated_omm_home, installed, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(agent_connection, "preflight", lambda model, base: {**META, "model": model, "base_url": base})
    calls = []
    monkeypatch.setattr(agent_launch, "run", lambda argv, env: calls.append((argv, env)) or 7)
    return home, calls


def test_cli_launches_without_writing_settings_and_remembers_model(launched, monkeypatch):
    home, calls = launched
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    result = CliRunner().invoke(cli.app, ["code", "codex", "--model", "qwen3:8b", "exec", "--json", "hi"])

    assert result.exit_code == 7, result.output
    [(argv, env)] = calls
    assert argv[-3:] == ["exec", "--json", "hi"]
    assert 'model="qwen3:8b"' in _overrides(argv)
    assert not any(home.iterdir())  # no ~/.codex, ~/.claude or backup written
    assert not (config.OMM_HOME / "agent-connections").exists()
    assert config.load_config()["agent_code_model"] == "qwen3:8b"
    assert "this session only" in result.output


def test_cli_off_a_terminal_reuses_the_last_model(launched, monkeypatch):
    _, calls = launched
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    config.update_config(agent_code_model="qwen3:8b")

    result = CliRunner().invoke(cli.app, ["code", "claude", "--", "-p", "hi"])

    assert result.exit_code == 7, result.output
    assert calls[0][0][1:] == ["--model", "qwen3:8b", "-p", "hi"]


def test_cli_picker_starts_at_the_last_model(launched, monkeypatch):
    _, calls = launched
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(agent_connection, "installed_models", lambda *a, **k: ["a:1", "qwen3:8b"])
    config.update_config(agent_code_model="qwen3:8b")
    selects = []
    monkeypatch.setattr(questionary, "select", lambda *a, **k: selects.append(k) or k)
    monkeypatch.setattr(cli, "_ask_select", lambda question: "a:1")

    result = CliRunner().invoke(cli.app, ["code", "codex"])

    assert result.exit_code == 7, result.output
    assert selects[0]["default"] == "qwen3:8b"
    assert config.load_config()["agent_code_model"] == "a:1"


def test_cli_rejection_starts_nothing(launched, monkeypatch):
    _, calls = launched

    def refuse(model, base):
        raise agent_connection.ConnectionError("This model does not advertise tool calling.")

    monkeypatch.setattr(agent_connection, "preflight", refuse)
    result = CliRunner().invoke(cli.app, ["code", "codex", "--model", "qwen3:8b"])

    assert result.exit_code == 1
    assert "tool calling" in result.output
    assert calls == []
    assert "agent_code_model" not in config.load_config()


def test_cli_missing_harness_off_a_terminal_lists_only_supported_ones(launched, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli.app, ["code"])
    assert result.exit_code == 2
    assert "Missing harness: claude or codex." in result.output
