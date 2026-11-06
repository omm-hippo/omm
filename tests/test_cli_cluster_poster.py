"""`omm cluster poster enable|disable`, its status line, and its place in leave/update."""

from __future__ import annotations

import json
import types

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.cluster import commands, launchd, poster_config, poster_runtime, poster_service, settings

runner = CliRunner()
TOKEN = "k" * 43
ENV = (f"SUPABASE_URL=https://x.supabase.co\nSUPABASE_ANON_KEY=anon\nSITE_URL=https://dshs.dev\n"
       f"TOKEN={TOKEN}\n")


class _Fakes:
    def __init__(self):
        self.tokens = []
        self.deleted_tokens = 0
        self.installs = 0
        self.uninstalls = 0
        self.venv_builds = 0
        self.venv = False
        self.in_cluster = True


@pytest.fixture
def fakes(monkeypatch, isolated_omm_home):
    f = _Fakes()
    monkeypatch.setattr(commands, "require_macos", lambda: None)
    monkeypatch.setattr(settings, "load", lambda: types.SimpleNamespace(role="member") if f.in_cluster else None)
    monkeypatch.setattr(poster_config, "store_token", lambda token, **_k: f.tokens.append(token))
    monkeypatch.setattr(poster_config, "delete_token",
                        lambda **_k: setattr(f, "deleted_tokens", f.deleted_tokens + 1))
    monkeypatch.setattr(poster_runtime, "venv_ready", lambda **_k: f.venv)

    def build(**_k):
        f.venv_builds += 1
        f.venv = True

    monkeypatch.setattr(poster_runtime, "build_venv", build)
    plist = isolated_omm_home / "LaunchAgents" / "com.omm.cluster-poster.plist"
    monkeypatch.setattr(launchd, "plist_path", lambda label: isolated_omm_home / "LaunchAgents" / f"{label}.plist")

    def install(**_k):
        f.installs += 1
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("x", encoding="utf-8")

    def uninstall(**_k):
        f.uninstalls += 1
        plist.unlink(missing_ok=True)

    monkeypatch.setattr(poster_service, "install", install)
    monkeypatch.setattr(poster_service, "uninstall", uninstall)
    monkeypatch.setattr(poster_service, "is_running", lambda **_k: True)
    return f


def test_enable_refuses_outside_cluster(fakes):
    fakes.in_cluster = False
    result = runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    assert result.exit_code == 1
    assert "not in a cluster" in result.output
    assert fakes.tokens == [] and fakes.installs == 0


def test_enable_from_stdin_env(fakes):
    result = runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    assert result.exit_code == 0, result.output
    assert fakes.tokens == [TOKEN]
    assert poster_config.load_settings() == poster_config.PosterSettings("https://x.supabase.co", "anon",
                                                                         "https://dshs.dev")
    assert fakes.venv_builds == 1 and fakes.installs == 1
    assert TOKEN not in result.output
    assert "15 GB" in result.output


def test_enable_from_env_file(fakes, tmp_path):
    path = tmp_path / "poster.env"
    path.write_text(ENV, encoding="utf-8")
    result = runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", str(path)])
    assert result.exit_code == 0, result.output
    assert fakes.tokens == [TOKEN]


def test_enable_rejects_http_url(fakes):
    result = runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"],
                           input=ENV.replace("https://dshs.dev", "http://dshs.dev"))
    assert result.exit_code == 1
    assert "https" in result.output and fakes.tokens == [] and fakes.installs == 0


def test_enable_twice_replaces(fakes):
    runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    other = "z" * 43
    result = runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"],
                           input=ENV.replace(TOKEN, other))
    assert result.exit_code == 0, result.output
    assert fakes.tokens == [TOKEN, other]
    assert fakes.installs == 2 and fakes.venv_builds == 1


def test_disable_removes_everything(fakes):
    runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    (poster_config.poster_dir() / "venv").mkdir(parents=True)
    result = runner.invoke(cli.app, ["cluster", "poster", "disable"])
    assert result.exit_code == 0, result.output
    assert fakes.uninstalls == 1 and fakes.deleted_tokens == 1
    assert poster_config.load_settings() is None
    assert not (poster_config.poster_dir() / "venv").exists()
    assert not poster_service.is_installed()


def test_disable_when_nothing_installed(fakes):
    result = runner.invoke(cli.app, ["cluster", "poster", "disable"])
    assert result.exit_code == 0, result.output
    assert fakes.uninstalls == 0 and fakes.deleted_tokens == 0


def test_poster_status_report(fakes):
    assert poster_service.status() == {"installed": False, "running": False, "state": None,
                                       "line": "Poster art: off"}
    runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    from omm.cluster import poster_worker

    poster_worker.write_state(phase="downloading the model")
    report = poster_service.status()
    assert report["installed"] and report["running"]
    assert report["line"] == "Poster art: on — downloading the model"
    assert TOKEN not in json.dumps(report)


def test_leave_runs_poster_disable(fakes, monkeypatch):
    calls = []
    monkeypatch.setattr(poster_service, "disable", lambda **_k: calls.append("disable"))
    for name in ("firewall", "netconfig", "power", "appfw", "service", "member_service", "rag_backup",
                 "exo_service", "preflight", "members"):
        module = getattr(commands, name)
        for attr in ("remove", "restore", "uninstall", "delete_backup", "delete_snapshot", "remove_runtime",
                     "clear", "clear_handover"):
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, lambda *a, **k: None)
    monkeypatch.setattr(settings, "clear", lambda: None)
    commands.leave(lambda _line: None)
    assert calls == ["disable"]


def test_restart_if_installed(fakes, monkeypatch):
    restarts = []
    monkeypatch.setattr(launchd, "restart_agent", lambda label, **_k: restarts.append(label))
    poster_service.restart_if_installed()
    assert restarts == []
    runner.invoke(cli.app, ["cluster", "poster", "enable", "--env-file", "-"], input=ENV)
    poster_service.restart_if_installed()
    assert restarts == ["com.omm.cluster-poster"]


def test_hidden_run_command_in_skip_sets():
    for name in ("_SKIP_UPDATE_CHECK_SUBCOMMANDS", "_SKIP_ONBOARDING_SUBCOMMANDS", "_SKIP_AUTO_IMPORT_SUBCOMMANDS",
                 "_SKIP_QUEUED_UPLOAD_SUBCOMMANDS", "_INTERNAL_SUBCOMMANDS"):
        assert "_cluster-poster-run" in getattr(cli, name), name


def test_poster_job_runs_the_hidden_subcommand(isolated_omm_home):
    job = poster_service._job()
    assert job["ProgramArguments"][-1] == "_cluster-poster-run"
    assert job["KeepAlive"] is True
    assert job["StandardOutPath"].endswith("logs/cluster-poster.log")
