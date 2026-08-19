from __future__ import annotations

import json

from omm import config


def test_unaccepted_legacy_firebase_default_migrates_to_local(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_opt_in": False,
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] is None
    assert loaded["telemetry_backend"] == "local"


def test_explicit_legacy_firebase_opt_in_is_preserved(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_opt_in": True,
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == config.LEGACY_FIREBASE_ENDPOINT
    assert loaded["telemetry_backend"] == "firebase_legacy"


def test_telemetry_opt_in_true_migrates_to_always_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"telemetry_opt_in": True}))

    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "always"
    assert "telemetry_opt_in" not in loaded


def test_telemetry_opt_in_false_migrates_to_ask_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"telemetry_opt_in": False}))

    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert "telemetry_opt_in" not in loaded


def test_fresh_config_defaults_to_ask_policy(isolated_omm_home):
    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert loaded["contribute_always_ack"] is False


def test_legacy_model_url_migrates_to_current_default(isolated_omm_home):
    for legacy_url in config.LEGACY_MODEL_URLS:
        config.CONFIG_PATH.write_text(json.dumps({"model_url": legacy_url}))

        loaded = config.load_config()

        assert loaded["model_url"] == config.DEFAULT_CONFIG["model_url"]


def test_legacy_manifest_url_migrates_to_current_default(isolated_omm_home):
    for legacy_url in config.LEGACY_MANIFEST_URLS:
        config.CONFIG_PATH.write_text(json.dumps({"catalog_manifest_url": legacy_url}))

        loaded = config.load_config()

        assert loaded["catalog_manifest_url"] == config.DEFAULT_CONFIG["catalog_manifest_url"]


def test_custom_model_url_is_preserved(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps({"model_url": "https://example.com/custom-model.json"})
    )

    loaded = config.load_config()

    assert loaded["model_url"] == "https://example.com/custom-model.json"
