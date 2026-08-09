import json
from pathlib import Path

import pytest

from omm import linker, registry, scan_import


def _mock_symlinks(monkeypatch) -> set[Path]:
    """Model symlink semantics without requiring Windows Developer Mode."""
    marked: set[Path] = set()
    native_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.absolute() in marked or native_is_symlink(path),
    )
    return marked


def _all_linked(**overrides) -> dict:
    linked = {spec.key: False for spec in linker.ENGINES}
    linked.update(overrides)
    return linked


def _write_manifest(manifests_root, namespace, name, tag, digest_hex, size=100):
    manifest_dir = manifests_root / "registry.ollama.ai" / namespace / name
    manifest_dir.mkdir(parents=True)
    (manifest_dir / tag).write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{'c' * 64}", "size": 1},
                "layers": [
                    {
                        "mediaType": "application/vnd.ollama.image.model",
                        "digest": f"sha256:{digest_hex}",
                        "size": size,
                    }
                ],
            }
        )
    )


def test_scan_ollama_skips_config_blobs_and_symlinks(tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    blobs_dir.mkdir(parents=True)

    model_digest = "a" * 64
    (blobs_dir / f"sha256-{model_digest}").write_bytes(b"gguf-bytes")

    # config blob shares the same sha256-<hash> naming but isn't a model layer
    config_digest = "c" * 64
    (blobs_dir / f"sha256-{config_digest}").write_bytes(b"{}")

    # already-symlinked model blob (previously adopted) must be skipped
    symlinked_digest = "b" * 64
    linked_target = tmp_path / "already-in-hub.gguf"
    linked_target.write_bytes(b"x")
    linked_blob = blobs_dir / f"sha256-{symlinked_digest}"
    linked_blob.write_bytes(b"x")
    _mock_symlinks(monkeypatch).add(linked_blob.absolute())

    _write_manifest(manifests_root, "library", "llama3", "latest", model_digest)
    _write_manifest(manifests_root, "library", "already-linked", "latest", symlinked_digest)

    monkeypatch.setattr(scan_import.linker, "ollama_models_dir", lambda: models_dir)

    found = scan_import.scan_ollama()

    assert len(found) == 1
    assert found[0].sha256 == model_digest
    assert found[0].display_name == "llama3:latest"
    assert found[0].engine == "ollama"


def test_scan_lmstudio_skips_symlinks(tmp_path, monkeypatch):
    models_dir = tmp_path / "lmstudio" / "models"
    real_dir = models_dir / "org" / "repo"
    real_dir.mkdir(parents=True)
    real_file = real_dir / "model.gguf"
    real_file.write_bytes(b"gguf-bytes")

    link_dir = models_dir / "org2" / "repo2"
    link_dir.mkdir(parents=True)
    linked_file = link_dir / "linked.gguf"
    linked_file.write_bytes(b"linked")
    _mock_symlinks(monkeypatch).add(linked_file.absolute())

    monkeypatch.setattr(scan_import.linker, "lmstudio_models_dir", lambda: models_dir)

    found = scan_import.scan_lmstudio()

    assert len(found) == 1
    assert found[0].path == real_file
    assert found[0].display_name == "model.gguf"


def test_scan_anythingllm_reuses_ollama_format_at_its_own_dir(tmp_path, monkeypatch):
    models_dir = tmp_path / "anythingllm-ollama"
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    blobs_dir.mkdir(parents=True)

    model_digest = "a" * 64
    (blobs_dir / f"sha256-{model_digest}").write_bytes(b"gguf-bytes")
    _write_manifest(manifests_root, "library", "llama3", "latest", model_digest)

    monkeypatch.setattr(scan_import.linker, "anythingllm_ollama_models_dir", lambda: models_dir)

    found = scan_import.scan_anythingllm()

    assert len(found) == 1
    assert found[0].engine == "anythingllm"
    assert found[0].sha256 == model_digest


def test_scan_mstystudio_and_textgenwebui_and_koboldcpp_find_flat_files(tmp_path, monkeypatch):
    mocked_links = _mock_symlinks(monkeypatch)
    for engine, scan_fn, attr in (
        ("mstystudio", scan_import.scan_mstystudio, "mstystudio_models_dir"),
        ("textgenwebui", scan_import.scan_textgenwebui, "textgenwebui_models_dir"),
        ("koboldcpp", scan_import.scan_koboldcpp, "koboldcpp_models_dir"),
    ):
        models_dir = tmp_path / engine
        models_dir.mkdir()
        real_file = models_dir / "model.gguf"
        real_file.write_bytes(b"gguf-bytes")
        linked_file = models_dir / "linked.gguf"
        linked_file.write_bytes(b"linked")
        mocked_links.add(linked_file.absolute())

        monkeypatch.setattr(scan_import.linker, attr, lambda d=models_dir: d)

        found = scan_fn()

        assert len(found) == 1, engine
        assert found[0].path == real_file
        assert found[0].engine == engine


def test_scan_textgenwebui_and_koboldcpp_return_empty_when_not_found(monkeypatch):
    monkeypatch.setattr(scan_import.linker, "textgenwebui_models_dir", lambda: None)
    monkeypatch.setattr(scan_import.linker, "koboldcpp_models_dir", lambda: None)

    assert scan_import.scan_textgenwebui() == []
    assert scan_import.scan_koboldcpp() == []


def test_scan_jan_resolves_absolute_and_relative_model_paths(tmp_path, monkeypatch):
    jan_app_dir = tmp_path / "Jan"
    jan_data_dir = jan_app_dir / "data"
    models_dir = jan_data_dir / "llamacpp" / "models"
    monkeypatch.setattr(scan_import.linker, "jan_app_dir", lambda: jan_app_dir)
    monkeypatch.setattr(scan_import.linker, "jan_models_dir", lambda: models_dir)

    absolute_gguf = tmp_path / "somewhere" / "abs-model.gguf"
    absolute_gguf.parent.mkdir(parents=True)
    absolute_gguf.write_bytes(b"abs-bytes")
    (models_dir / "abs-entry").mkdir(parents=True)
    (models_dir / "abs-entry" / "model.yml").write_text(f'model_path: "{absolute_gguf}"\nname: "abs-entry"\n')

    rel_gguf = jan_data_dir / "llamacpp" / "models" / "rel-entry" / "model.gguf"
    rel_gguf.parent.mkdir(parents=True)
    rel_gguf.write_bytes(b"rel-bytes")
    (models_dir / "rel-entry" / "model.yml").write_text(
        'model_path: "llamacpp/models/rel-entry/model.gguf"\nname: "rel-entry"\n'
    )

    # already-adopted entry (model_path resolves to a symlink) must be skipped
    already_linked_target = tmp_path / "hub-model.gguf"
    already_linked_target.write_bytes(b"hub-bytes")
    (models_dir / "already-linked").mkdir(parents=True)
    already_linked = models_dir / "already-linked" / "model.gguf"
    already_linked.write_bytes(b"hub-bytes")
    _mock_symlinks(monkeypatch).add(already_linked.absolute())
    (models_dir / "already-linked" / "model.yml").write_text(
        f'model_path: "{models_dir / "already-linked" / "model.gguf"}"\nname: "already-linked"\n'
    )

    found = scan_import.scan_jan()

    by_name = {item.display_name: item for item in found}
    assert set(by_name) == {"abs-entry", "rel-entry"}
    assert by_name["abs-entry"].path == absolute_gguf
    assert by_name["rel-entry"].path == rel_gguf
    assert all(item.engine == "jan" for item in found)


def test_group_by_hash_merges_identical_files_across_engines(tmp_path):
    a = scan_import.ExternalGguf("ollama", "llama3:latest", tmp_path / "a", 10, "same-hash")
    b = scan_import.ExternalGguf("lmstudio", "model.gguf", tmp_path / "b", 10, "same-hash")
    c = scan_import.ExternalGguf("lmstudio", "other.gguf", tmp_path / "c", 5, "different-hash")

    groups = scan_import.group_by_hash([a, b, c])

    by_hash = {g.sha256: g for g in groups}
    assert len(groups) == 2
    assert sorted(loc.engine for loc in by_hash["same-hash"].locations) == ["lmstudio", "ollama"]
    assert by_hash["same-hash"].display_name == "model.gguf"  # prefers the real LM Studio filename
    assert by_hash["different-hash"].engines == ["lmstudio"]


def test_adopt_group_merges_duplicate_across_engines_and_reports_saved_bytes(isolated_omm_home, tmp_path):
    payload = b"identical gguf bytes"

    ollama_path = tmp_path / "ollama-blob"
    ollama_path.write_bytes(payload)
    lmstudio_dir = tmp_path / "lmstudio" / "org" / "repo"
    lmstudio_dir.mkdir(parents=True)
    lmstudio_path = lmstudio_dir / "model.gguf"
    lmstudio_path.write_bytes(payload)

    group = scan_import.ModelGroup(
        sha256="deadbeef",
        locations=[
            scan_import.ExternalGguf("ollama", "llama3:latest", ollama_path, len(payload), "deadbeef"),
            scan_import.ExternalGguf("lmstudio", "model.gguf", lmstudio_path, len(payload), "deadbeef"),
        ],
    )

    result = scan_import.adopt_group(group)

    hub_path = scan_import.MODELS_DIR / "model.gguf"
    assert hub_path.exists() and not hub_path.is_symlink()
    assert hub_path.read_bytes() == payload
    assert ollama_path.samefile(hub_path)
    assert lmstudio_path.samefile(hub_path)
    assert result.bytes_saved == len(payload)  # one of the two copies reclaimed

    entry = registry.load_registry()["model.gguf"]
    assert entry["sha256"] == "deadbeef"
    assert entry["linked"] == _all_linked(lmstudio=True, ollama=True)


def test_adopt_group_reuses_existing_hub_copy_for_same_hash(isolated_omm_home, tmp_path):
    payload = b"already installed via omm"
    hub_file = scan_import.MODELS_DIR / "existing.gguf"
    hub_file.write_bytes(payload)
    registry.upsert_entry(
        "existing.gguf",
        sha256="cafef00d",
        version="cafef00",
        source="https://example.com/existing.gguf",
        size_bytes=len(payload),
        installed_at="2026-01-01T00:00:00+00:00",
        ollama_name="existing",
        repo_id="org/repo",
        linked={"lmstudio": True, "ollama": False},
    )

    stray_dir = tmp_path / "ollama-stray"
    stray_dir.mkdir()
    stray_path = stray_dir / "sha256-cafef00d"
    stray_path.write_bytes(payload)

    group = scan_import.ModelGroup(
        sha256="cafef00d",
        locations=[scan_import.ExternalGguf("ollama", "existing:latest", stray_path, len(payload), "cafef00d")],
    )

    result = scan_import.adopt_group(group)

    assert result.filename == "existing.gguf"
    assert result.bytes_saved == len(payload)
    assert stray_path.samefile(hub_file)

    entry = registry.load_registry()["existing.gguf"]
    assert entry["linked"] == _all_linked(lmstudio=True, ollama=True)  # merged, lmstudio flag preserved


def test_adopt_group_preserves_duplicate_changed_after_scan(isolated_omm_home, tmp_path):
    hub_file = scan_import.MODELS_DIR / "existing.gguf"
    hub_file.write_bytes(b"trusted hub bytes")
    registry.upsert_entry("existing.gguf", sha256="scan-hash", linked={})
    external = tmp_path / "external.gguf"
    external.write_bytes(b"changed after scan")
    group = scan_import.ModelGroup(
        sha256="scan-hash",
        locations=[scan_import.ExternalGguf("lmstudio", "external.gguf", external, external.stat().st_size, "scan-hash")],
    )

    with pytest.raises(linker.LinkError, match="changed unowned duplicate"):
        scan_import.adopt_group(group)
    assert external.read_bytes() == b"changed after scan"


def test_adopt_group_restores_duplicate_when_link_creation_fails(isolated_omm_home, tmp_path, monkeypatch):
    payload = b"identical bytes"
    hub_file = scan_import.MODELS_DIR / "existing.gguf"
    hub_file.write_bytes(payload)
    registry.upsert_entry("existing.gguf", sha256="scan-hash", linked={})
    external = tmp_path / "external.gguf"
    external.write_bytes(payload)
    group = scan_import.ModelGroup(
        sha256="scan-hash",
        locations=[scan_import.ExternalGguf("lmstudio", "external.gguf", external, len(payload), "scan-hash")],
    )
    monkeypatch.setattr(scan_import.linker, "link_file", lambda *_: (_ for _ in ()).throw(linker.LinkError("cross-drive")))

    with pytest.raises(linker.LinkError, match="cross-drive"):
        scan_import.adopt_group(group)
    assert external.read_bytes() == payload
    assert not list(tmp_path.glob(".external.gguf.omm-import-*"))
