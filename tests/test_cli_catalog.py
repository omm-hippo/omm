import json

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


def test_load_config_migrates_on_disk_null_catalog_config_to_signed_defaults(isolated_omm_home):
    """A config.json written before this branch always had
    catalog_manifest_url/catalog_public_key explicitly set to null (they were
    already in DEFAULT_CONFIG, just with None values). Without a migration,
    _merge_config's {**DEFAULT_CONFIG, **data} spread keeps those on-disk
    nulls forever, silently defeating signature verification for every
    pre-existing install."""
    config.CONFIG_PATH.write_text(
        json.dumps({"catalog_manifest_url": None, "catalog_public_key": None})
    )

    loaded = config.load_config()

    assert loaded["catalog_manifest_url"] == config.DEFAULT_CONFIG["catalog_manifest_url"]
    assert loaded["catalog_public_key"] == config.DEFAULT_CONFIG["catalog_public_key"]


def test_load_config_does_not_override_explicit_catalog_trust(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "catalog_manifest_url": "https://example.com/manifest.json",
                "catalog_public_key": "custom-key",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["catalog_manifest_url"] == "https://example.com/manifest.json"
    assert loaded["catalog_public_key"] == "custom-key"


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
