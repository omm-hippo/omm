import errno

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


def test_main_reraises_non_enospc_oserror_unchanged(monkeypatch):
    def _raise_permission_denied():
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(cli, "app", _raise_permission_denied)

    with pytest.raises(OSError) as exc_info:
        cli.main()
    assert exc_info.value.errno == errno.EACCES


def test_main_reraises_other_exceptions_unchanged(monkeypatch):
    def _raise_value_error():
        raise ValueError("some genuine bug")

    monkeypatch.setattr(cli, "app", _raise_value_error)

    with pytest.raises(ValueError):
        cli.main()
