import json
from pathlib import Path

import pytest

from omm import linker


def test_link_ollama_refuses_clip_mmproj_gguf(tmp_path, monkeypatch):
    """mmproj GGUFs (general.architecture == 'clip') are vision projectors,
    not standalone text-generation models. Ollama's llama-server crashes with
    'unsupported model architecture: clip' if asked to run one alone, so omm
    must refuse the link rather than create a manifest for a model that can
    never generate text."""
    gguf_path = tmp_path / "mmproj.gguf"
    gguf_path.write_bytes(b"not a real gguf, metadata is mocked below")
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda path, keys: {"general.architecture": "clip"})
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: tmp_path / "ollama")

    with pytest.raises(linker.LinkError, match="multimodal projector"):
        linker.link_ollama(gguf_path, "mmproj")

    assert not (tmp_path / "ollama").exists()


def test_autoremove_lmstudio_deletes_broken_symlink_and_prunes_empty_dirs(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "lmstudio" / "models"
    broken_dir = models_dir / "TheBloke" / "TinyLlama-1.1B-Chat-v1.0-GGUF"
    broken_dir.mkdir(parents=True)
    broken_link = broken_dir / "tinyllama.gguf"
    try:
        broken_link.symlink_to(tmp_path / "does-not-exist.gguf")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation on this Windows host")
    linker._record_symlink(broken_link, tmp_path / "does-not-exist.gguf")

    live_target = tmp_path / "real.gguf"
    live_target.write_bytes(b"data")
    live_dir = models_dir / "org" / "repo"
    live_dir.mkdir(parents=True)
    live_link = live_dir / "real.gguf"
    live_link.symlink_to(live_target)

    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: models_dir)

    removed = linker.autoremove_lmstudio()

    assert removed == 1
    assert not broken_link.is_symlink()
    assert not broken_dir.exists()  # emptied parent got pruned
    assert live_link.is_symlink()  # untouched


def test_autoremove_lmstudio_returns_zero_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_lmstudio() == 0


def test_autoremove_ollama_removes_broken_blob_and_its_manifest(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)

    broken_digest_hex = "a" * 64
    broken_blob = blobs_dir / f"sha256-{broken_digest_hex}"
    try:
        broken_blob.symlink_to(tmp_path / "gone.gguf")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation on this Windows host")
    linker._record_symlink(broken_blob, tmp_path / "gone.gguf")

    live_digest_hex = "b" * 64
    live_blob = blobs_dir / f"sha256-{live_digest_hex}"
    live_target = tmp_path / "alive.gguf"
    live_target.write_bytes(b"data")
    live_blob.symlink_to(live_target)

    manifests_root = models_dir / "manifests" / "registry.ollama.ai" / "library"

    broken_manifest_dir = manifests_root / "broken-model"
    broken_manifest_dir.mkdir(parents=True)
    broken_manifest = broken_manifest_dir / "latest"
    broken_manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{broken_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{broken_digest_hex}", "size": 1}],
            }
        )
    )
    linker._record_ownership(broken_manifest, None, "manifest")

    live_manifest_dir = manifests_root / "alive-model"
    live_manifest_dir.mkdir(parents=True)
    (live_manifest_dir / "latest").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{live_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{live_digest_hex}", "size": 1}],
            }
        )
    )

    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)

    blobs_removed, manifests_removed = linker.autoremove_ollama()

    assert blobs_removed == 1
    assert manifests_removed == 1
    assert not broken_blob.exists()
    assert not (broken_manifest_dir / "latest").exists()
    assert live_blob.is_symlink()
    assert (live_manifest_dir / "latest").exists()


def test_autoremove_ollama_preserves_unowned_broken_blob_and_manifest(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)
    digest = "c" * 64
    blob = blobs_dir / f"sha256-{digest}"
    try:
        blob.symlink_to(tmp_path / "gone.gguf")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation on this Windows host")
    manifest = models_dir / "manifests" / "registry.ollama.ai" / "library" / "user" / "latest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"layers": [{"digest": f"sha256:{digest}"}]}))

    assert linker.autoremove_ollama(models_dir=models_dir) == (0, 0)
    assert blob.is_symlink()
    assert manifest.exists()


def test_autoremove_ollama_returns_zero_when_blobs_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_ollama() == (0, 0)


def test_autoremove_ollama_skips_manifest_it_cannot_unlink(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)

    broken_digest_hex = "a" * 64
    broken_blob = blobs_dir / f"sha256-{broken_digest_hex}"
    try:
        broken_blob.symlink_to(tmp_path / "gone.gguf")
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or elevation on this Windows host")
    linker._record_symlink(broken_blob, tmp_path / "gone.gguf")

    manifests_root = models_dir / "manifests" / "registry.ollama.ai" / "library"
    broken_manifest_dir = manifests_root / "broken-model"
    broken_manifest_dir.mkdir(parents=True)
    broken_manifest = broken_manifest_dir / "latest"
    broken_manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{broken_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{broken_digest_hex}", "size": 1}],
            }
        )
    )
    linker._record_ownership(broken_manifest, None, "manifest")

    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)
    real_unlink = Path.unlink

    def _flaky_unlink(self, missing_ok=False):
        if self == broken_manifest:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    blobs_removed, manifests_removed = linker.autoremove_ollama()  # must not raise

    assert blobs_removed == 1
    assert manifests_removed == 0
    assert broken_manifest.exists()
