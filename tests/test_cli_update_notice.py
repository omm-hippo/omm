import sys

from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


class _FakeCtx:
    def __init__(self, invoked_subcommand):
        self.invoked_subcommand = invoked_subcommand
        self.close_callbacks = []

    def call_on_close(self, fn):
        self.close_callbacks.append(fn)


def test_bg_version_check_cmd_delegates_to_cached_remote_head(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.version_check,
        "cached_remote_head",
        lambda fetch, *a, **k: calls.append(fetch) or "new_sha",
    )

    cli._bg_version_check_cmd()

    assert calls == [cli._remote_head_commit]


def test_confirm_and_print_update_notice_prints_when_live_check_confirms(monkeypatch):
    """The cached mismatch is only a hint; a live re-check confirming it is
    what actually earns the notice."""
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "new_sha")
    monkeypatch.setattr(cli.version_check, "record", lambda *a, **k: None)
    printed = []
    monkeypatch.setattr(cli.err_console, "print", lambda *a, **k: printed.append(a))

    cli._confirm_and_print_update_notice("new_sha", "old_sha")

    assert printed
    assert "omm update" in printed[0][0]


def test_confirm_and_print_update_notice_silent_when_live_check_shows_current(monkeypatch):
    """Cache said "new_sha" was newer, but the live re-check shows the
    installed commit already caught up - this is the stale-cache case from
    the bug report and must stay silent."""
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "old_sha")
    recorded = []
    monkeypatch.setattr(cli.version_check, "record", lambda v, *a, **k: recorded.append(v))
    printed = []
    monkeypatch.setattr(cli.err_console, "print", lambda *a, **k: printed.append(a))

    cli._confirm_and_print_update_notice("new_sha", "old_sha")

    assert printed == []
    assert recorded == ["old_sha"]


def test_confirm_and_print_update_notice_silent_when_offline(monkeypatch):
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": None)
    monkeypatch.setattr(
        cli.version_check, "record", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no record"))
    )
    printed = []
    monkeypatch.setattr(cli.err_console, "print", lambda *a, **k: printed.append(a))

    cli._confirm_and_print_update_notice("new_sha", "old_sha")

    assert printed == []


def test_maybe_start_update_check_skips_for_update_subcommand(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no popen")))
    ctx = _FakeCtx("update")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_for_help_subcommand(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no popen")))
    ctx = _FakeCtx("help")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_for_own_bg_check_subcommand(monkeypatch):
    """The hidden `_bg-version-check` child must not recursively try to
    spawn another check on itself."""
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no popen")))
    ctx = _FakeCtx("_bg-version-check")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_for_dev_install(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no popen")))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_confirms_when_fresh_cache_has_newer_version(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli.version_check, "cached_remote_head_if_fresh", lambda *a, **k: (True, "new_sha"))
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "new_sha")
    monkeypatch.setattr(cli.version_check, "record", lambda *a, **k: None)
    printed = []
    monkeypatch.setattr(cli.err_console, "print", lambda *a, **k: printed.append(a))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert len(ctx.close_callbacks) == 1
    ctx.close_callbacks[0]()
    assert printed
    assert "omm update" in printed[0][0]


def test_maybe_start_update_check_silent_when_fresh_cache_matches_installed(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "same_sha")
    monkeypatch.setattr(cli.version_check, "cached_remote_head_if_fresh", lambda *a, **k: (True, "same_sha"))
    monkeypatch.setattr(
        cli, "_remote_head_commit", lambda ref="main": (_ for _ in ()).throw(AssertionError("no live check"))
    )
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []
    assert printed == []


def test_maybe_start_update_check_spawns_detached_child_when_stale_and_not_in_flight(monkeypatch):
    """Cache is stale and nobody else is already checking: this short
    command must not block on the network - it only kicks off a detached
    child and registers no notice of its own (the result isn't known yet)."""
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli.version_check, "cached_remote_head_if_fresh", lambda *a, **k: (False, None))
    monkeypatch.setattr(cli.version_check, "should_start_check", lambda *a, **k: True)
    marked = []
    monkeypatch.setattr(cli.version_check, "mark_checking", lambda *a, **k: marked.append(1))
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: popen_calls.append((args, kwargs)))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert marked == [1]
    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert args == [sys.executable, "-m", "omm.cli", "_bg-version-check"]
    assert kwargs["start_new_session"] is True
    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_spawn_when_check_already_in_flight(monkeypatch):
    """Several short `omm` commands run back to back shouldn't each spawn
    their own `git ls-remote` child while one is already in flight."""
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli.version_check, "cached_remote_head_if_fresh", lambda *a, **k: (False, None))
    monkeypatch.setattr(cli.version_check, "should_start_check", lambda *a, **k: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no popen")))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_end_to_end_shows_update_notice_on_a_later_short_command(isolated_omm_home, monkeypatch):
    """The check is split across invocations: the first short command that
    finds a stale cache only kicks off the detached child (mocked here to
    run synchronously in-process, standing in for it finishing after the
    parent has already exited); a later short command then sees the fresh
    cache and prints the notice."""
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "new_sha")

    def fake_popen(args, **kwargs):
        cli._bg_version_check_cmd()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    first = runner.invoke(cli.app, ["list"])
    assert first.exit_code == 0, first.stdout
    assert "update available" not in first.stdout.lower()

    second = runner.invoke(cli.app, ["list"])
    assert second.exit_code == 0, second.stdout
    assert "omm update" in second.stderr


def test_end_to_end_silent_when_already_up_to_date(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "same_sha")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "same_sha")

    def fake_popen(args, **kwargs):
        cli._bg_version_check_cmd()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    first = runner.invoke(cli.app, ["list"])
    second = runner.invoke(cli.app, ["list"])

    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout
    assert "update available" not in first.stdout.lower()
    assert "update available" not in second.stdout.lower()


def test_end_to_end_update_subcommand_does_not_trigger_background_check(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1, result.stdout
