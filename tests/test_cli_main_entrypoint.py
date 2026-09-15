import errno
import os
import sys

import pytest

from omm import cli


def test_main_prints_friendly_message_on_enospc_oserror_and_exits_1(monkeypatch, capsys):
    def _raise_enospc():
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cli, "app", _raise_enospc)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "disk space" in captured.err.lower()


def test_main_prints_friendly_message_on_insufficient_disk_space_error(monkeypatch, capsys):
    def _raise_disk_space_error():
        raise cli.InsufficientDiskSpaceError("model.gguf needs 5.0GB but only 1.0GB free")

    monkeypatch.setattr(cli, "app", _raise_disk_space_error)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "5.0GB" in captured.err


def test_main_prints_friendly_message_on_permission_error_and_exits_1(monkeypatch, capsys):
    def _raise_permission_denied():
        raise OSError(errno.EACCES, "Permission denied", "/some/path")

    monkeypatch.setattr(cli, "app", _raise_permission_denied)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "/some/path" in captured.err
    assert "permission" in captured.err.lower()
    assert "→" in captured.err


def test_main_reraises_non_permission_non_enospc_oserror_unchanged(monkeypatch):
    def _raise_other_oserror():
        raise OSError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(cli, "app", _raise_other_oserror)

    with pytest.raises(OSError) as exc_info:
        cli.main()
    assert exc_info.value.errno == errno.ECONNREFUSED


def test_main_reraises_other_exceptions_unchanged(monkeypatch):
    def _raise_value_error():
        raise ValueError("some genuine bug")

    monkeypatch.setattr(cli, "app", _raise_value_error)

    with pytest.raises(ValueError):
        cli.main()


def test_main_sets_no_default_cwd_in_exe_path(monkeypatch):
    """A planted exe in an untrusted cwd must not shadow the real one for a
    bare executable name (git, pipx, ollama, ...) that a child process might
    launch - see the comment above the `os.environ.setdefault(...)` call in
    `cli.main()`."""
    # setenv first so monkeypatch records and restores the variable afterward.
    monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "x")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath")
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        cli.main()

    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_main_does_not_override_existing_no_default_cwd_in_exe_path_env(monkeypatch):
    monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "0")
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        cli.main()

    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "0"


def test_registered_command_names_match_typers_real_names():
    import typer.main

    assert cli._REGISTERED_COMMAND_NAMES == frozenset(typer.main.get_command(cli.app).commands)


def test_hidden_background_command_is_a_known_subcommand():
    from omm import runlog

    assert "_bg-version-check" in cli._REGISTERED_COMMAND_NAMES
    assert runlog.subcommand_of(["_bg-version-check"]) == "_bg-version-check"
    assert runlog.subcommand_of(["totally-unknown"]) == "unknown"


def test_internal_background_subcommand_is_not_counted_in_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.usage, "record_run", lambda *a: calls.append(a))
    monkeypatch.setattr(cli, "app", lambda: None)
    monkeypatch.setattr(sys, "argv", ["omm", "_bg-version-check"])
    cli.main()
    assert calls == []


def test_user_subcommand_is_still_counted_in_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.usage, "record_run", lambda *a: calls.append(a))
    monkeypatch.setattr(cli, "app", lambda: None)
    monkeypatch.setattr(sys, "argv", ["omm", "search"])
    cli.main()
    assert calls == [("search", "ok", None)]


def test_ctrl_c_inside_click_is_recorded_as_interrupted(monkeypatch):
    import click

    recorded = []
    monkeypatch.setattr(cli.usage, "record_run", lambda cmd, outcome, exc: recorded.append(outcome))

    def _click_style_abort():
        # Reproduces what click.Command.main does: KeyboardInterrupt -> Abort -> sys.exit(1)
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt as e:
            abort = click.exceptions.Abort()
            abort.__cause__ = e
            try:
                raise abort
            except click.exceptions.Abort:
                raise SystemExit(1)

    monkeypatch.setattr(cli, "app", _click_style_abort)
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 1
    assert recorded == ["interrupted"]


def test_eof_abort_is_still_failed(monkeypatch):
    import click

    recorded = []
    monkeypatch.setattr(cli.usage, "record_run", lambda cmd, outcome, exc: recorded.append(outcome))

    def _click_style_abort():
        try:
            raise EOFError
        except EOFError as e:
            abort = click.exceptions.Abort()
            abort.__cause__ = e
            try:
                raise abort
            except click.exceptions.Abort:
                raise SystemExit(1)

    monkeypatch.setattr(cli, "app", _click_style_abort)
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 1
    assert recorded == ["failed"]


@pytest.mark.parametrize(
    ("exit_code", "expected_outcome"),
    [(1, "failed"), (2, "usage-error"), (0, "ok")],
)
def test_plain_exit_is_still_mapped_the_same_way(monkeypatch, exit_code, expected_outcome):
    recorded = []
    monkeypatch.setattr(cli.usage, "record_run", lambda cmd, outcome, exc: recorded.append(outcome))
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(SystemExit(exit_code)))
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == exit_code
    assert recorded == [expected_outcome]
