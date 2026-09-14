import errno
import os

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


def test_main_sets_no_default_cwd_in_exe_path_on_windows(monkeypatch):
    """A planted exe in an untrusted cwd must not shadow the real one for a
    bare executable name (git, pipx, ollama, ...) that a child process might
    launch - see the comment above the `os.environ.setdefault(...)` call in
    `cli.main()`."""
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        cli.main()

    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_main_does_not_override_existing_no_default_cwd_in_exe_path_env(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "0")
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        cli.main()

    assert os.environ["NoDefaultCurrentDirectoryInExePath"] == "0"
