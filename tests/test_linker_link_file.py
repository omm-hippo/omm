import struct
from pathlib import Path

import pytest

from omm import linker


def test_link_file_creates_symlink_by_default(isolated_omm_home, tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    linker.link_file(src, dst)

    # Native Windows without Developer Mode falls back to a hard link.
    assert dst.is_symlink() or dst.samefile(src)
    assert dst.read_bytes() == b"weights"


def test_link_file_falls_back_to_hardlink_on_windows_when_symlink_fails(isolated_omm_home, tmp_path, monkeypatch):
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


def test_link_file_replaces_existing_destination(isolated_omm_home, tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"stale")

    with pytest.raises(linker.LinkError, match="unowned"):
        linker.link_file(src, dst)
    assert dst.read_bytes() == b"stale"


def _force_windows_hardlinks(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda self, target: (_ for _ in ()).throw(OSError("symlink privilege required")),
    )


def test_windows_hardlink_lifecycle_removes_only_recorded_link(isolated_omm_home, tmp_path, monkeypatch):
    _force_windows_hardlinks(monkeypatch)
    source = tmp_path / "hub" / "model.gguf"
    source.parent.mkdir()
    source.write_bytes(b"weights")
    directory = tmp_path / "custom"
    destination = linker.link_custom_directory(source, directory)

    assert destination.exists() and not destination.is_symlink()
    linker.unlink_custom_directory(source.name, directory)
    assert not destination.exists()

    # A file recreated by another program at the same managed-looking path is
    # not the inode recorded by omm and must never be removed during cleanup.
    destination.write_bytes(b"someone else's model")
    linker.unlink_custom_directory(source.name, directory)
    assert destination.read_bytes() == b"someone else's model"


def test_windows_hardlink_lmstudio_and_ollama_uninstall_lifecycle(isolated_omm_home, tmp_path, monkeypatch):
    _force_windows_hardlinks(monkeypatch)
    source = tmp_path / "hub.gguf"
    source.write_bytes(b"weights")

    lmstudio = tmp_path / "lmstudio" / "models"
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: lmstudio)
    lm_destination = linker.link_lmstudio(source, "org/repo")
    linker.unlink_lmstudio(source.name, "org/repo")
    assert not lm_destination.exists()

    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    linker.link_ollama(source, "model", models_dir=models_dir)
    blob = next((models_dir / "blobs").iterdir())
    assert not blob.is_symlink()
    linker.unlink_ollama("model", models_dir=models_dir)
    assert blob.exists()  # content-addressed blobs can be shared; Ollama GC owns them


def test_autoremove_preserves_registered_orphan_hardlink(isolated_omm_home, tmp_path, monkeypatch):
    _force_windows_hardlinks(monkeypatch)
    source = tmp_path / "hub" / "model.gguf"
    source.parent.mkdir()
    source.write_bytes(b"weights")
    directory = tmp_path / "custom"
    destination = linker.link_custom_directory(source, directory)
    source.unlink()

    assert linker.autoremove_custom_directory(directory) == 0
    assert destination.exists()


def test_link_file_refuses_to_replace_unowned_regular_file(isolated_omm_home, tmp_path):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    destination = tmp_path / "destination.gguf"
    destination.write_bytes(b"user data")

    with pytest.raises(linker.LinkError, match="unowned"):
        linker.link_file(source, destination)
    assert destination.read_bytes() == b"user data"


def test_relink_adopts_matching_pre_ownership_hardlink(isolated_omm_home, tmp_path, monkeypatch):
    _force_windows_hardlinks(monkeypatch)
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    destination = tmp_path / "models" / "source.gguf"
    linker.link_file(source, destination)
    # Simulate a link created before link-ownership.json existed.
    linker._update_link_ownership(destination, None)

    linker.link_file(source, destination)

    assert destination.samefile(source)
    assert linker.unlink_owned_link(destination)
    assert not destination.exists()


def test_ollama_preserves_unowned_manifest_and_existing_blob(isolated_omm_home, tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    digest = linker.sha256_file(source)
    blob = models_dir / "blobs" / f"sha256-{digest}"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"user blob")
    manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("user manifest")

    with pytest.raises(linker.LinkError, match="unowned Ollama manifest"):
        linker.link_ollama(source, "model", models_dir=models_dir)
    assert blob.read_bytes() == b"user blob"
    assert manifest.read_text() == "user manifest"


def test_link_file_raises_link_error_when_mkdir_fails(tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError("permission denied")))

    with pytest.raises(linker.LinkError):
        linker.link_file(src, dst)


def test_link_ollama_raises_link_error_when_blob_write_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    monkeypatch.setattr(
        Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(linker.LinkError):
        linker.link_ollama(source, "model", models_dir=models_dir)


def test_link_ollama_raises_link_error_on_corrupt_gguf_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"

    def _raise_struct_error(*a, **k):
        raise struct.error("unpack requires a buffer of 8 bytes")

    monkeypatch.setattr(linker, "read_gguf_metadata", _raise_struct_error)

    with pytest.raises(linker.LinkError, match="corrupted"):
        linker.link_ollama(source, "model", models_dir=models_dir)
