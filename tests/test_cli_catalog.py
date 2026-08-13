from typer.testing import CliRunner

from omm import catalog, cli, config

runner = CliRunner()


def test_catalog_signing_is_on_by_default():
    assert config.DEFAULT_CONFIG["catalog_manifest_url"] == (
        "https://raw.githubusercontent.com/omm-hippo/omm/main/published/recommend-model.manifest.json"
    )
    public_key = config.DEFAULT_CONFIG["catalog_public_key"]
    assert public_key is not None
    # Must be a valid Ed25519 public key, not just a non-empty string.
    catalog.public_key_fingerprint(public_key)


def test_catalog_trust_saves_verified_public_key(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(catalog, "public_key_fingerprint", lambda key: "abcd1234")

    result = runner.invoke(
        cli.app,
        ["setting", "catalog-trust", "--manifest-url", "https://example.com/manifest.json", "--public-key", "key"],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["catalog_manifest_url"] == "https://example.com/manifest.json"
    assert saved["catalog_public_key"] == "key"
