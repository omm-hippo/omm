import json
from pathlib import Path

import pytest

from scripts import sign_catalog
from omm import catalog, config

ROOT = Path(__file__).resolve().parents[1]


def test_sign_catalog_output_is_accepted_by_the_catalog_verifier(tmp_path):
    private_path = tmp_path / "signing.key"
    public_path = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private_path, public_path)

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}', encoding="utf-8")
    manifest_path = tmp_path / "recommend-model.manifest.json"

    public_key = public_path.read_text(encoding="utf-8").strip()
    sign_catalog.sign(artifact, private_path, manifest_path, public_key=public_key)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    verified = catalog.verify_signed_artifact(artifact.read_bytes(), manifest, public_key)

    assert verified["artifact_sha256"] == manifest["artifact_sha256"]


def test_sign_catalog_rejects_verification_with_the_wrong_key(tmp_path):
    sign_catalog.generate_keys(tmp_path / "a.key", tmp_path / "a.pub")
    sign_catalog.generate_keys(tmp_path / "b.key", tmp_path / "b.pub")

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}', encoding="utf-8")
    manifest_path = tmp_path / "recommend-model.manifest.json"
    a_public_key = (tmp_path / "a.pub").read_text(encoding="utf-8").strip()
    sign_catalog.sign(artifact, tmp_path / "a.key", manifest_path, public_key=a_public_key)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wrong_public_key = (tmp_path / "b.pub").read_text(encoding="utf-8").strip()

    try:
        catalog.verify_signed_artifact(artifact.read_bytes(), manifest, wrong_public_key)
        raised = False
    except catalog.CatalogVerificationError:
        raised = True
    assert raised


def test_generate_keys_refuses_to_overwrite_existing_private_key(tmp_path):
    private = tmp_path / "signing.key"
    public = tmp_path / "signing.pub"
    private.write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        sign_catalog.generate_keys(private, public)

    assert private.read_text(encoding="utf-8") == "preserve"
    assert not public.exists()


def test_sign_catalog_rejects_non_object_json(tmp_path):
    private = tmp_path / "signing.key"
    public = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private, public)
    artifact = tmp_path / "recommend-model.json"
    artifact.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        sign_catalog.sign(artifact, private, tmp_path / "manifest.json")


def test_sign_refuses_a_key_that_does_not_match_the_published_public_key(tmp_path):
    private = tmp_path / "signing.key"
    public = tmp_path / "signing.pub"
    sign_catalog.generate_keys(private, public)

    artifact = tmp_path / "recommend-model.json"
    artifact.write_text('{"candidates": []}', encoding="utf-8")
    manifest_path = tmp_path / "recommend-model.manifest.json"

    with pytest.raises(ValueError):
        sign_catalog.sign(artifact, private, manifest_path)

    assert not manifest_path.exists()


def test_published_catalog_verifies_with_the_shipped_public_key():
    artifact = ROOT / "published" / "localfit-recommend-model.json"
    manifest_path = ROOT / "published" / "localfit-recommend-model.manifest.json"
    content = artifact.read_bytes().replace(b"\r\n", b"\n")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog.verify_signed_artifact(content, manifest, config.DEFAULT_CONFIG["catalog_public_key"])
