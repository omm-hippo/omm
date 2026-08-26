from __future__ import annotations

from pathlib import Path

from omm import recommend_status, registry, scan_import


def _candidate(**overrides):
    candidate = {
        "name": "unsloth-gpt-oss-20b-gguf",
        "repo_id": "unsloth/gpt-oss-20b-GGUF",
        "filename": "gpt-oss-20b-Q4_K_M.gguf",
        "provider": "huggingface",
    }
    candidate.update(overrides)
    return candidate


def test_detects_exact_existing_omm_install(isolated_omm_home, monkeypatch):
    filename = "gpt-oss-20b-Q4_K_M.gguf"
    model = isolated_omm_home / "models" / filename
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"gguf")
    registry.save_registry(
        {
            filename: {
                "repo_id": "unsloth/gpt-oss-20b-GGUF",
                "provider": "huggingface",
                "linked": {"ollama": True, "lmstudio": True},
            }
        }
    )
    monkeypatch.setattr(scan_import, "find_external_model_identities", lambda: [])

    [status] = recommend_status.detect_installation_statuses([_candidate()])

    assert status.installed is True
    assert status.managed_by_omm is True
    assert status.engines == ("ollama", "lmstudio")
    assert status.managed_filename == filename
    assert status.match_kind == "exact"


def test_stale_registry_entry_is_not_reported_as_installed(
    isolated_omm_home, monkeypatch
):
    registry.save_registry(
        {
            "gpt-oss-20b-Q4_K_M.gguf": {
                "repo_id": "unsloth/gpt-oss-20b-GGUF",
                "provider": "huggingface",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(scan_import, "find_external_model_identities", lambda: [])

    [status] = recommend_status.detect_installation_statuses([_candidate()])

    assert status == recommend_status.NOT_INSTALLED


def test_same_filename_from_different_repository_is_not_a_managed_match(
    isolated_omm_home, monkeypatch
):
    filename = "gpt-oss-20b-Q4_K_M.gguf"
    model = isolated_omm_home / "models" / filename
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"other")
    registry.save_registry(
        {
            filename: {
                "repo_id": "different/repository",
                "provider": "huggingface",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(scan_import, "find_external_model_identities", lambda: [])

    [status] = recommend_status.detect_installation_statuses([_candidate()])

    assert status == recommend_status.NOT_INSTALLED


def test_detects_legacy_omm_model_by_exact_model_identity(
    isolated_omm_home, monkeypatch
):
    filename = "qwen3.5-9b.gguf"
    model = isolated_omm_home / "models" / filename
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"imported")
    registry.save_registry(
        {
            filename: {
                "repo_id": None,
                "provider": None,
                "ollama_name": "qwen3.5-9b",
                "linked": {"ollama": True},
            }
        }
    )
    monkeypatch.setattr(scan_import, "find_external_model_identities", lambda: [])
    candidate = {
        "repo_id": "unsloth/Qwen3.5-9B-GGUF",
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "provider": "huggingface",
    }

    [status] = recommend_status.detect_installation_statuses([candidate])

    assert status.installed is True
    assert status.managed_by_omm is True
    assert status.engines == ("ollama",)
    assert status.managed_filename == filename
    assert status.match_kind == "model_identity"


def test_detects_exact_external_lmstudio_file(isolated_omm_home, monkeypatch):
    identity = scan_import.ExternalModelIdentity(
        "lmstudio",
        "gpt-oss-20b-Q4_K_M.gguf",
        Path("/models/unsloth/gpt-oss-20b-GGUF/gpt-oss-20b-Q4_K_M.gguf"),
    )
    monkeypatch.setattr(
        scan_import, "find_external_model_identities", lambda: [identity]
    )

    [status] = recommend_status.detect_installation_statuses([_candidate()])

    assert status.installed is True
    assert status.managed_by_omm is False
    assert status.engines == ("lmstudio",)
    assert status.match_kind == "exact"


def test_detects_exact_ollama_tag_but_not_different_role_variant(
    isolated_omm_home, monkeypatch
):
    exact = scan_import.ExternalModelIdentity(
        "ollama",
        "gpt-oss-20b-q4_k_m:latest",
        Path("/ollama/blobs/sha256-exact"),
    )
    different_variant = scan_import.ExternalModelIdentity(
        "ollama",
        "gpt-oss-coder:20b",
        Path("/ollama/blobs/sha256-similar"),
    )
    monkeypatch.setattr(
        scan_import, "find_external_model_identities", lambda: [exact]
    )
    [exact_status] = recommend_status.detect_installation_statuses([_candidate()])

    monkeypatch.setattr(
        scan_import, "find_external_model_identities", lambda: [different_variant]
    )
    [different_status] = recommend_status.detect_installation_statuses([_candidate()])

    assert exact_status.engines == ("ollama",)
    assert exact_status.match_kind == "exact"
    assert different_status == recommend_status.NOT_INSTALLED


def test_detects_same_model_identity_across_ollama_tag_and_gguf_packaging(
    isolated_omm_home, monkeypatch
):
    candidate = {
        "name": "qwen3.5-9b-gguf",
        "repo_id": "unsloth/Qwen3.5-9B-GGUF",
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "provider": "huggingface",
    }
    identity = scan_import.ExternalModelIdentity(
        "ollama",
        "qwen3.5:9b",
        Path("/ollama/blobs/sha256-qwen"),
    )
    monkeypatch.setattr(
        scan_import, "find_external_model_identities", lambda: [identity]
    )

    [status] = recommend_status.detect_installation_statuses([candidate])

    assert status.installed is True
    assert status.managed_by_omm is False
    assert status.engines == ("ollama",)
    assert status.match_kind == "model_identity"


def test_lightweight_flat_scan_does_not_hash_model_bytes(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"large-model-placeholder")
    monkeypatch.setattr(
        scan_import,
        "sha256_file",
        lambda path: (_ for _ in ()).throw(AssertionError("must not hash")),
    )

    identities = scan_import._scan_flat_dir_identities("lmstudio", tmp_path)

    assert identities == [
        scan_import.ExternalModelIdentity("lmstudio", "model.gguf", model)
    ]
