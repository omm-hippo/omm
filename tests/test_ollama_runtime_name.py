import json

from omm import linker


def _write_manifest(models_dir, *, name, tag, digest, namespace="library"):
    path = (
        models_dir
        / "manifests"
        / "registry.ollama.ai"
        / namespace
        / name
        / tag
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "layers": [
                    {
                        "mediaType": "application/vnd.ollama.image.model",
                        "digest": f"sha256:{digest}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_resolve_legacy_imported_name_by_exact_manifest_digest(tmp_path):
    digest = "a" * 64
    _write_manifest(tmp_path, name="qwen3", tag="4b", digest=digest)

    resolved = linker.resolve_ollama_runtime_name(
        "qwen3-4b.gguf",
        {"ollama_name": "qwen3-4b", "sha256": digest},
        models_dir=tmp_path,
    )

    assert resolved == "qwen3:4b"


def test_explicit_imported_runtime_name_wins_without_manifest_scan(tmp_path):
    resolved = linker.resolve_ollama_runtime_name(
        "qwen3-4b.gguf",
        {
            "ollama_name": "qwen3-4b",
            "ollama_runtime_name": "qwen3:4b",
            "sha256": "a" * 64,
        },
        models_dir=tmp_path,
    )

    assert resolved == "qwen3:4b"


def test_ambiguous_digest_uses_unique_filename_safe_match(tmp_path):
    digest = "b" * 64
    _write_manifest(tmp_path, name="qwen3", tag="4b", digest=digest)
    _write_manifest(tmp_path, name="alias", tag="latest", digest=digest)

    resolved = linker.resolve_ollama_runtime_name(
        "qwen3-4b.gguf",
        {"ollama_name": "qwen3-4b", "sha256": digest},
        models_dir=tmp_path,
    )

    assert resolved == "qwen3:4b"


def test_ambiguous_digest_without_safe_match_keeps_existing_link_name(tmp_path):
    digest = "c" * 64
    _write_manifest(tmp_path, name="first", tag="v1", digest=digest)
    _write_manifest(tmp_path, name="second", tag="v2", digest=digest)

    resolved = linker.resolve_ollama_runtime_name(
        "legacy.gguf",
        {"ollama_name": "legacy", "sha256": digest},
        models_dir=tmp_path,
    )

    assert resolved == "legacy"


def test_nonlibrary_namespace_is_preserved(tmp_path):
    digest = "d" * 64
    _write_manifest(
        tmp_path,
        namespace="acme",
        name="llama3",
        tag="latest",
        digest=digest,
    )

    resolved = linker.resolve_ollama_runtime_name(
        "acme-llama3-latest.gguf",
        {"ollama_name": "acme-llama3-latest", "sha256": digest},
        models_dir=tmp_path,
    )

    assert resolved == "acme/llama3:latest"
