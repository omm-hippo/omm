from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omm import catalog, compare, quality_catalog


def document(**overrides):
    row = {
        "provider": "huggingface",
        "repo_id": "org/model",
        "filename": "model-Q4_K_M.gguf",
        "model_digest": "a" * 64,
        "task": "Coding",
        "pack_id": "coding",
        "pack_version": "1.0.0",
        "score": 0.75,
        "summary": "3/4 tasks solved",
        "source": "official",
    }
    row.update(overrides)
    return {"schema_version": 1, "generated_at": "2026-09-17T00:00:00+00:00", "evaluations": [row]}


def sign(payload: dict):
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = {
        "schema_version": 1,
        "artifact": "model-quality.json",
        "artifact_sha256": catalog.sha256_bytes(content),
        "signature": base64.b64encode(private.sign(content)).decode(),
    }
    return content, manifest, base64.b64encode(public).decode()


def test_signed_catalog_builds_exact_package_index():
    content, manifest, public = sign(document())
    index = quality_catalog.load_signed(content, manifest, public)
    [evidence] = index[("huggingface", "org/model", "model-q4_k_m.gguf")]
    assert evidence.task == "Coding"
    assert evidence.score == 0.75
    assert evidence.model_digest == "a" * 64


def test_bad_signature_and_duplicate_task_fail_closed(tmp_path):
    content, manifest, public = sign(document())
    manifest["artifact_sha256"] = "0" * 64
    with pytest.raises(quality_catalog.QualityCatalogError, match="verification"):
        quality_catalog.load_signed(content, manifest, public)

    payload = document()
    payload["evaluations"].append(dict(payload["evaluations"][0]))
    with pytest.raises(quality_catalog.QualityCatalogError, match="duplicate"):
        quality_catalog.build_index(payload)


def test_cached_catalog_missing_or_invalid_returns_no_evidence(tmp_path):
    assert quality_catalog.load_cached("key", artifact_path=tmp_path / "missing", manifest_path=tmp_path / "missing-manifest") == {}
    artifact = tmp_path / "quality.json"
    manifest = tmp_path / "manifest.json"
    artifact.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    assert quality_catalog.load_cached("key", artifact_path=artifact, manifest_path=manifest) == {}


@pytest.mark.parametrize("field,value", [
    ("score", 1.1),
    ("model_digest", "bad"),
    ("source", "community"),
    ("task", "Gaming"),
    ("filename", "../model.gguf"),
])
def test_invalid_quality_rows_are_rejected(field, value):
    with pytest.raises(quality_catalog.QualityCatalogError):
        quality_catalog.build_index(document(**{field: value}))
