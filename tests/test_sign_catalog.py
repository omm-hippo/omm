import json

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
