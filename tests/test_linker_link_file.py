from pathlib import Path
from types import SimpleNamespace

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


def test_link_file_copies_when_symlink_and_hardlink_fail_on_windows(
    isolated_omm_home, tmp_path, monkeypatch
):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no perm")))
    monkeypatch.setattr(
        Path, "hardlink_to", lambda self, target: (_ for _ in ()).throw(OSError("cross-drive"))
    )

    assert linker.link_file(src, dst) == "copy"
    assert dst.read_bytes() == b"weights"
    assert linker.unlink_owned_link(dst)
    assert not dst.exists()


def test_windows_copy_fallback_reports_extra_disk_usage(
    isolated_omm_home, tmp_path, monkeypatch
):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "other-volume" / "model.gguf"
    reports = []

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        Path, "hardlink_to", lambda self, target: (_ for _ in ()).throw(OSError("cross-drive"))
    )
    monkeypatch.setattr(
        Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no privilege"))
    )

    assert linker.link_file(src, dst, on_copy=lambda *args: reports.append(args)) == "copy"
    assert reports == [(src, dst, len(b"weights"))]


def test_windows_copy_fallback_refuses_insufficient_space_without_partial_file(
    isolated_omm_home, tmp_path, monkeypatch
):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "other-volume" / "model.gguf"

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        Path, "hardlink_to", lambda self, target: (_ for _ in ()).throw(OSError("cross-drive"))
    )
    monkeypatch.setattr(
        Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no privilege"))
    )
    monkeypatch.setattr(linker.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))

    with pytest.raises(linker.LinkError, match="destination has only"):
        linker.link_file(src, dst)
    assert not dst.exists()


def test_link_engine_surfaces_windows_copy_warning(isolated_omm_home, tmp_path, monkeypatch):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    models_dir = tmp_path / "MstyStudio" / "models"

    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker, "mstystudio_models_dir", lambda: models_dir)
    monkeypatch.setattr(
        Path, "hardlink_to", lambda self, target: (_ for _ in ()).throw(OSError("cross-drive"))
    )
    monkeypatch.setattr(
        Path, "symlink_to", lambda self, target: (_ for _ in ()).throw(OSError("no privilege"))
    )

    warning = linker.link_engine("mstystudio", src, repo_id=None, ollama_tag="model")

    assert warning is not None
    assert "copied" in warning
    assert "additional disk space" in warning
    assert (models_dir / src.name).read_bytes() == b"weights"


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
    blob = models_dir / "blobs" / f"sha256-{linker.sha256_file(source)}"
    assert not blob.is_symlink()
    linker.unlink_ollama("model", models_dir=models_dir)
    assert not blob.exists()  # no remaining manifest references this omm-owned blob


def test_windows_owned_blob_delete_retries_transient_file_lock(monkeypatch, tmp_path):
    calls = []

    def flaky_unlink(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("mapped file still open")
        return True

    monkeypatch.setattr(linker, "unlink_owned_link", flaky_unlink)
    monkeypatch.setattr(linker.time, "sleep", lambda seconds: None)

    assert linker._unlink_owned_link_with_retry(tmp_path / "blob") is True
    assert len(calls) == 3


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
