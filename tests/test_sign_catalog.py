import json

import pytest

from scripts import sign_catalog
from omm import catalog


def test_sign_catalog_output_is_accepted_by_the_catalog_verifier(tmp_path):
    private_path = tmp_path / "signing.key"
    public_path = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private_path, public_path)

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}')
    manifest_path = tmp_path / "recommend-model.manifest.json"

    sign_catalog.sign(artifact, private_path, manifest_path)

    manifest = json.loads(manifest_path.read_text())
    public_key = public_path.read_text().strip()

    verified = catalog.verify_signed_artifact(artifact.read_bytes(), manifest, public_key)

    assert verified["artifact_sha256"] == manifest["artifact_sha256"]


def test_sign_catalog_rejects_verification_with_the_wrong_key(tmp_path):
    sign_catalog.generate_keys(tmp_path / "a.key", tmp_path / "a.pub")
    sign_catalog.generate_keys(tmp_path / "b.key", tmp_path / "b.pub")

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}')
    manifest_path = tmp_path / "recommend-model.manifest.json"
    sign_catalog.sign(artifact, tmp_path / "a.key", manifest_path)

    manifest = json.loads(manifest_path.read_text())
    wrong_public_key = (tmp_path / "b.pub").read_text().strip()

    try:
        catalog.verify_signed_artifact(artifact.read_bytes(), manifest, wrong_public_key)
        raised = False
    except catalog.CatalogVerificationError:
        raised = True
    assert raised


def test_generate_keys_refuses_to_overwrite_existing_private_key(tmp_path):
    private = tmp_path / "signing.key"
    public = tmp_path / "signing.pub"
    private.write_text("preserve")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        sign_catalog.generate_keys(private, public)

    assert private.read_text() == "preserve"
    assert not public.exists()


def test_sign_catalog_rejects_non_object_json(tmp_path):
    private = tmp_path / "signing.key"
    public = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private, public)
    artifact = tmp_path / "recommend-model.json"
    artifact.write_text("[]")

    with pytest.raises(ValueError, match="JSON object"):
        sign_catalog.sign(artifact, private, tmp_path / "manifest.json")
