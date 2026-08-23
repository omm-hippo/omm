from __future__ import annotations

import base64
import errno
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omm import catalog


def _keys():
    private = Ed25519PrivateKey.generate()
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public_raw).decode()


def test_signed_catalog_verification_accepts_exact_artifact():
    private, public = _keys()
    content = b'{"model_version":1}'
    manifest = {
        "schema_version": 1,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "signature": base64.b64encode(private.sign(content)).decode(),
    }

    assert catalog.verify_signed_artifact(content, manifest, public) == manifest

    with pytest.raises(catalog.CatalogVerificationError):
        catalog.verify_signed_artifact(content + b"x", manifest, public)


def test_catalog_rollback_restores_previous_different_snapshot(tmp_path):
    artifact = tmp_path / "recommend.json"
    history = tmp_path / "history"
    artifact.write_text('{"version":1}')
    catalog.archive_current_artifact(artifact, history)
    artifact.write_text('{"version":2}')

    selected = catalog.rollback(artifact_path=artifact, history_dir=history)

    assert selected.exists()
    assert artifact.read_text() == '{"version":1}'


def test_signed_rollback_requires_and_restores_signature_provenance(tmp_path):
    private, public = _keys()
    artifact = tmp_path / "recommend.json"
    history = tmp_path / "history"
    content = b'{"version":1}'
    artifact.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "signature": base64.b64encode(private.sign(content)).decode(),
    }
    provenance = artifact.with_suffix(".json.provenance.json")
    provenance.write_text(json.dumps({"manifest": manifest, "public_key": public}))
    assert catalog.archive_current_artifact(artifact, history, require_signed=True)
    artifact.write_text('{"version":2}')
    provenance.unlink()

    selected = catalog.rollback(
        artifact_path=artifact, history_dir=history, require_signed=True
    )

    assert selected.read_bytes() == content
    restored = json.loads(provenance.read_text())
    assert catalog.verify_signed_artifact(content, restored["manifest"], restored["public_key"])


def test_archive_current_artifact_returns_none_on_write_failure(tmp_path, monkeypatch):
    artifact = tmp_path / "recommend.json"
    artifact.write_text('{"version":1}')
    history = tmp_path / "history"

    monkeypatch.setattr(
        Path, "mkdir", lambda self, parents=True, exist_ok=True: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
    )

    assert catalog.archive_current_artifact(artifact, history) is None
