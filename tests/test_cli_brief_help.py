import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli, cli_help


def test_missing_engine_subcommand_is_short_and_points_to_help():
    result = CliRunner().invoke(cli.app, ["engine"])
    assert result.exit_code == 2
    assert "Missing command" in result.stderr
    assert "engine --help" in result.stderr
    assert len(result.stderr.splitlines()) <= 3


def test_bad_argument_in_json_mode_is_one_parseable_error():
    result = CliRunner().invoke(cli.app, ["scan", "--bogus", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["error"]["code"] == "invalid_arguments"
    assert "bogus" in data["error"]["message"]
    assert result.stderr == ""


def test_json_flag_after_separator_is_a_literal_argument():
    assert not cli_help.option_requested(["scan", "--", "--json"], "--json")


def test_hint_is_once_per_command_and_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_help, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), stderr=SimpleNamespace(isatty=lambda: True)))
    path = tmp_path / "tty-a.json"
    monkeypatch.setattr(cli_help.session_cache, "_session_path", lambda: path)
    assert cli_help.first_terminal_hint("omm engine") is True
    assert cli_help.first_terminal_hint("omm engine") is False
    assert cli_help.first_terminal_hint("omm install") is True
    path = tmp_path / "tty-b.json"
    assert cli_help.first_terminal_hint("omm engine") is True


def test_pipe_never_creates_a_help_session(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_help, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False), stderr=SimpleNamespace(isatty=lambda: False)))
    monkeypatch.setattr(cli_help.session_cache, "_session_path", lambda: tmp_path / "tty.json")
    assert cli_help.first_terminal_hint("omm engine") is False
    assert list(tmp_path.iterdir()) == []


def test_engine_readonly_detection_does_not_match_a_model_named_engine():
    assert cli_help.engine_read_only_args(["--json", "engine", "status", "ollama"])
    assert not cli_help.engine_read_only_args(["info", "engine", "status"])
