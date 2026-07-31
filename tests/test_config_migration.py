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
    assert loaded["onboarding_version"] == config.CURRENT_ONBOARDING_VERSION


def test_telemetry_opt_in_false_migrates_to_ask_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"telemetry_opt_in": False}))

    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert "telemetry_opt_in" not in loaded


def test_fresh_config_defaults_to_ask_policy(isolated_omm_home):
    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert loaded["contribute_always_ack"] is False
    assert loaded["onboarding_version"] == 0
    assert loaded["config_schema_version"] == config.CONFIG_SCHEMA_VERSION


def test_invalid_new_settings_fall_back_without_reopening_setup(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "config_schema_version": config.CONFIG_SCHEMA_VERSION,
                "onboarding_version": "broken",
                "default_engine": "unknown",
                "telemetry_send_policy": "sometimes",
                "update_channel": "nightly",
                "ui_mode": "huge",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["onboarding_version"] == config.CURRENT_ONBOARDING_VERSION
    assert loaded["default_engine"] is None
    assert loaded["telemetry_send_policy"] == "ask"
    assert loaded["update_channel"] == "stable"
    assert loaded["ui_mode"] == "compact"
