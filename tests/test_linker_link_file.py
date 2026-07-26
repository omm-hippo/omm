from pathlib import Path

import pytest

from omm import linker


def test_link_file_creates_symlink_by_default(tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    linker.link_file(src, dst)

    assert dst.is_symlink()
    assert dst.read_bytes() == b"weights"


def test_link_file_falls_back_to_hardlink_on_windows_when_symlink_fails(tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")

    def _raise(self, target):
        raise OSError("symlink privilege required")

    monkeypatch.setattr(Path, "symlink_to", _raise)

    linker.link_file(src, dst)

    assert not dst.is_symlink()
    assert dst.is_file()
    assert dst.read_bytes() == b"weights"


def test_link_file_raises_on_non_windows_without_trying_hardlink(tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no perm")))

    def _fail_hardlink(self, target):
        raise AssertionError("hardlink_to should not be attempted on non-Windows")

    monkeypatch.setattr(Path, "hardlink_to", _fail_hardlink)

    with pytest.raises(linker.LinkError):
        linker.link_file(src, dst)


def test_link_file_raises_when_both_symlink_and_hardlink_fail_on_windows(tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no perm")))
    monkeypatch.setattr(
        Path, "hardlink_to", lambda self, target: (_ for _ in ()).throw(OSError("cross-drive"))
    )

    with pytest.raises(linker.LinkError):
        linker.link_file(src, dst)


def test_link_file_replaces_existing_destination(tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"stale")

    linker.link_file(src, dst)

    assert dst.read_bytes() == b"weights"
