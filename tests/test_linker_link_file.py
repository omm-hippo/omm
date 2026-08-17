import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from omm import config, linker


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


def test_link_file_force_reclaims_unowned_destination(isolated_omm_home, tmp_path):
    src = tmp_path / "model.gguf"
    src.write_bytes(b"weights")
    dst = tmp_path / "dst" / "model.gguf"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"stale")

    linker.link_file(src, dst, force=True)

    assert dst.is_symlink() or dst.samefile(src)
    assert dst.read_bytes() == b"weights"


def _force_windows_hardlinks(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda self, target: (_ for _ in ()).throw(OSError("symlink privilege required")),
    )


def test_windows_hardlink_lifecycle_removes_only_recorded_link(isolated_omm_home, tmp_path, monkeypatch):
    _force_windows_hardlinks(monkeypatch)
    source = linker.MODELS_DIR / "model.gguf"
    source.parent.mkdir(exist_ok=True)
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
    source = linker.MODELS_DIR / "hub.gguf"
    source.parent.mkdir(parents=True, exist_ok=True)
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


def test_nested_filename_unlink_uses_flat_link_basename(
    isolated_omm_home, tmp_path
):
    source = linker.MODELS_DIR / "sub" / "model.gguf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"weights")
    directory = tmp_path / "custom"
    destination = linker.link_custom_directory(source, directory)

    linker.unlink_custom_directory("sub/model.gguf", directory)

    assert not destination.exists()


def test_flat_link_collision_does_not_replace_another_model(
    isolated_omm_home, tmp_path
):
    first = linker.MODELS_DIR / "a" / "model.gguf"
    second = linker.MODELS_DIR / "b" / "model.gguf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    directory = tmp_path / "custom"
    destination = linker.link_custom_directory(first, directory)

    with pytest.raises(linker.LinkError, match="different model"):
        linker.link_custom_directory(second, directory)

    assert destination.samefile(first)
    linker.unlink_custom_directory("b/model.gguf", directory)
    assert destination.exists()
    linker.unlink_custom_directory("a/model.gguf", directory)
    assert not destination.exists()


def test_lmstudio_rejects_unsafe_repository_id(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "model.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "lmstudio"
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: models_dir)

    with pytest.raises(linker.LinkError, match="Unsafe model repository"):
        linker.link_lmstudio(source, "../escape")

    assert not (tmp_path / "escape" / "model.gguf").exists()


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

    with pytest.raises(
        linker.LinkError,
        match="does not match its digest|unowned Ollama manifest",
    ):
        linker.link_ollama(source, "model", models_dir=models_dir)
    assert blob.read_bytes() == b"user blob"
    assert manifest.read_text() == "user manifest"


def test_ollama_force_preserves_unowned_manifest(isolated_omm_home, tmp_path, monkeypatch):
    """Force never turns an unproven manifest into an OMM-owned file."""
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("stale manifest, no ownership record")

    with pytest.raises(linker.LinkError, match="unowned Ollama manifest"):
        linker.link_ollama(source, "model", models_dir=models_dir, force=True)

    assert manifest.read_text() == "stale manifest, no ownership record"


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


# --- Ollama manifest-format compatibility check -----------------------
#
# link_ollama's compat probe only ever engages when the caller relies on
# the default models_dir (see verify_compat's docstring) - every test
# above passes models_dir explicitly, which forces it off regardless of
# whether a real `ollama` binary happens to be on the dev machine's PATH.
# These tests exercise the probe itself by monkeypatching ollama_models_dir
# so "the default" resolves into tmp_path instead of touching a real
# install, and faking subprocess.run so no real `ollama` process ever runs.


def _stub_ollama_env(monkeypatch, tmp_path, run_ollama):
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    monkeypatch.setattr(linker.subprocess, "run", run_ollama)
    return models_dir


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_native_ollama_import_refuses_to_start_without_model_plus_reserve(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    existing_manifest = (
        models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    )
    existing_manifest.parent.mkdir(parents=True, exist_ok=True)
    existing_manifest.write_text('{"existing": true}')
    linker._record_ownership(existing_manifest, None, "manifest")
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(linker.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    monkeypatch.setattr(
        linker.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    with pytest.raises(linker.InsufficientLinkSpaceError, match="real copy"):
        linker._fallback_to_native_create(source, "model", models_dir)

    assert existing_manifest.read_text() == '{"existing": true}'


def test_failed_native_ollama_import_removes_new_blobs_and_manifest(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    blob = models_dir / "blobs" / "sha256-native"
    manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"

    def run_ollama(cmd, **kwargs):
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"partial native copy")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"layers":[{"digest":"sha256:native"}]}')
        return _FakeResult(returncode=1, stderr="no space left on device")

    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(
        linker.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3)
    )
    monkeypatch.setattr(linker.subprocess, "run", run_ollama)

    with pytest.raises(linker.InsufficientLinkSpaceError, match="transaction files were removed"):
        linker._fallback_to_native_create(source, "model", models_dir)

    assert not blob.exists()
    assert not manifest.exists()


def test_successful_native_ollama_blob_is_reclaimed_on_unlink(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    blob = models_dir / "blobs" / "sha256-native"
    manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"

    def run_ollama(cmd, **kwargs):
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"native copy")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            '{"schemaVersion":2,"layers":[{"mediaType":"application/vnd.ollama.image.model",'
            '"digest":"sha256:native"}]}'
        )
        return _FakeResult(returncode=0)

    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(
        linker.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3)
    )
    monkeypatch.setattr(linker.subprocess, "run", run_ollama)

    linker._fallback_to_native_create(source, "model", models_dir)
    linker.unlink_ollama("model", models_dir=models_dir)

    assert not blob.exists()
    assert not manifest.exists()


def test_link_ollama_reclaims_manifest_left_by_native_fallback(
    isolated_omm_home, tmp_path, monkeypatch
):
    """A manifest written by _fallback_to_native_create is recorded with
    source=None (ollama create remaps the model layer to its own digest, so
    there is no gguf path to store). If Ollama later becomes compatible
    with omm's hand-rolled manifest again, link_ollama's normal path must
    still recognize that manifest as its own and overwrite it - not treat
    the missing source as proof of an unowned file forever."""
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})

    def run_ollama(cmd, **kwargs):
        manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            '{"schemaVersion":2,"layers":[{"mediaType":"application/vnd.ollama.image.model",'
            '"digest":"sha256:native"}]}'
        )
        return _FakeResult(returncode=0)

    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(
        linker.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3)
    )
    monkeypatch.setattr(linker.subprocess, "run", run_ollama)
    linker._fallback_to_native_create(source, "model", models_dir)

    # Now Ollama is compatible again, so a later install takes the normal
    # hand-rolled path and must be allowed to replace its own manifest.
    linker.link_ollama(source, "model", models_dir=models_dir)

    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    written = json.loads(manifest_path.read_text())
    assert written["layers"][0]["digest"] == f"sha256:{linker.sha256_file(source)}"


def test_link_ollama_skips_show_call_when_version_already_cached_compatible(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    calls = []

    def run_ollama(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:] == ["--version"]:
            return _FakeResult(stdout="ollama version is 9.9.9")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    models_dir = _stub_ollama_env(monkeypatch, tmp_path, run_ollama)
    cache_path = tmp_path / ".omm" / "ollama_manifest_compat.json"
    monkeypatch.setattr(config, "OMM_HOME", tmp_path / ".omm")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text('{"ollama_version": "ollama version is 9.9.9", "compatible": true}')

    result = linker.link_ollama(source, "model")

    assert result is False  # no tokenizer.chat_template in the stubbed metadata
    assert [c[1:] for c in calls] == [["--version"]]  # no "show" call - cache hit
    assert (models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest").exists()


def test_link_ollama_records_compatible_after_successful_show_probe(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")

    def run_ollama(cmd, **kwargs):
        if cmd[1:] == ["--version"]:
            return _FakeResult(stdout="ollama version is 9.9.9")
        if cmd[1] == "show":
            return _FakeResult(returncode=0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    _stub_ollama_env(monkeypatch, tmp_path, run_ollama)
    home = tmp_path / ".omm"
    monkeypatch.setattr(config, "OMM_HOME", home)

    linker.link_ollama(source, "model")

    cache = json.loads((home / "ollama_manifest_compat.json").read_text())
    assert cache == {"ollama_version": "ollama version is 9.9.9", "compatible": True}


def test_link_ollama_falls_back_to_native_create_when_show_rejects_manifest(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    create_calls = []
    subprocess_options = []

    def run_ollama(cmd, **kwargs):
        subprocess_options.append(kwargs)
        if cmd[1:] == ["--version"]:
            return _FakeResult(stdout="ollama version is 9.9.9")
        if cmd[1] == "show":
            return _FakeResult(returncode=1, stderr="Error: model 'model:latest' not found")
        if cmd[1] == "create":
            create_calls.append(cmd)
            native_blob = models_dir / "blobs" / "sha256-native"
            native_blob.parent.mkdir(parents=True, exist_ok=True)
            native_blob.write_bytes(b"native weights")
            manifest = (
                models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
            )
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                '{"layers":[{"mediaType":"application/vnd.ollama.image.model",'
                '"digest":"sha256:native"}]}'
            )
            return _FakeResult(returncode=0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    _stub_ollama_env(monkeypatch, tmp_path, run_ollama)
    home = tmp_path / ".omm"
    monkeypatch.setattr(config, "OMM_HOME", home)

    result = linker.link_ollama(source, "model")

    assert result is True
    assert len(create_calls) == 1
    assert subprocess_options
    assert all(options["encoding"] == "utf-8" for options in subprocess_options)
    assert all(options["errors"] == "replace" for options in subprocess_options)
    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    assert json.loads(manifest_path.read_text())["layers"][0]["digest"] == "sha256:native"
    # The rejected hand-written model/config blobs were removed before the
    # native import, so peak usage is one extra copy and no stale blob leaks.
    assert {path.name for path in (models_dir / "blobs").iterdir()} == {"sha256-native"}
    cache = json.loads((home / "ollama_manifest_compat.json").read_text())
    assert cache == {"ollama_version": "ollama version is 9.9.9", "compatible": False}
    # Ownership must be recorded even though omm never wrote this manifest
    # itself, or unlink_ollama/autoremove_ollama would refuse to clean it up.
    linker.unlink_ollama("model", models_dir=models_dir)
    assert not manifest_path.exists()
    assert not (models_dir / "blobs" / "sha256-native").exists()


def test_link_ollama_short_circuits_straight_to_fallback_when_already_known_bad(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    calls = []

    def run_ollama(cmd, **kwargs):
        calls.append(cmd[1] if len(cmd) > 1 else cmd[0])
        if cmd[1:] == ["--version"]:
            return _FakeResult(stdout="ollama version is 9.9.9")
        if cmd[1] == "create":
            manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text('{"native": true}')
            return _FakeResult(returncode=0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    models_dir = _stub_ollama_env(monkeypatch, tmp_path, run_ollama)
    home = tmp_path / ".omm"
    monkeypatch.setattr(config, "OMM_HOME", home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "ollama_manifest_compat.json").write_text(
        '{"ollama_version": "ollama version is 9.9.9", "compatible": false}'
    )

    result = linker.link_ollama(source, "model")

    assert result is True
    assert "show" not in calls  # never probes again once known bad
    assert "create" in calls


def test_link_ollama_treats_unreachable_daemon_as_unverified_not_incompatible(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")

    def run_ollama(cmd, **kwargs):
        if cmd[1:] == ["--version"]:
            return _FakeResult(stdout="ollama version is 9.9.9")
        if cmd[1] == "show":
            return _FakeResult(returncode=1, stderr="Error: could not connect to ollama server")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    models_dir = _stub_ollama_env(monkeypatch, tmp_path, run_ollama)
    home = tmp_path / ".omm"
    monkeypatch.setattr(config, "OMM_HOME", home)

    result = linker.link_ollama(source, "model")

    assert result is False  # no tokenizer.chat_template in the stubbed metadata
    assert not (home / "ollama_manifest_compat.json").exists()
    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / "model" / "latest"
    assert manifest_path.exists()  # omm's own hand-rolled manifest, untouched


def test_link_ollama_explicit_models_dir_never_calls_ollama_cli(
    isolated_omm_home, tmp_path, monkeypatch
):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/bin/ollama")

    def run_ollama(cmd, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(linker.subprocess, "run", run_ollama)

    result = linker.link_ollama(source, "model", models_dir=models_dir)

    assert result is False  # no tokenizer.chat_template in the stubbed metadata
