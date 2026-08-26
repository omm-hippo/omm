from __future__ import annotations

import json

import pytest

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


def test_explicit_legacy_firebase_opt_in_migrates_to_gateway(isolated_omm_home):
    """omm-hippo/omm#133 closed the direct RTDB path - a config still
    pointed at it, even one with a real opt-in, must move to the PoW-gated
    gateway or its telemetry silently stops landing anywhere."""
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_opt_in": True,
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == config.TELEMETRY_GATEWAY_ENDPOINT
    assert loaded["telemetry_backend"] == "gateway"


def test_already_migrated_firebase_legacy_config_moves_to_gateway(isolated_omm_home):
    """The common case: a config saved by an older omm version already has
    telemetry_backend == 'firebase_legacy' from a previous load, so the
    original-schema migration branch above never fires for it - this one
    must catch it independently."""
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_send_policy": "always",
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
                "telemetry_backend": "firebase_legacy",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == config.TELEMETRY_GATEWAY_ENDPOINT
    assert loaded["telemetry_backend"] == "gateway"


def test_self_hosted_endpoint_is_left_alone(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_send_policy": "always",
                "telemetry_endpoint": "https://telemetry.example.internal/v1/benchmarks",
                "telemetry_backend": "self_hosted",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == "https://telemetry.example.internal/v1/benchmarks"
    assert loaded["telemetry_backend"] == "self_hosted"


def test_already_gateway_config_is_idempotent(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_send_policy": "always",
                "telemetry_endpoint": config.TELEMETRY_GATEWAY_ENDPOINT,
                "telemetry_backend": "gateway",
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == config.TELEMETRY_GATEWAY_ENDPOINT
    assert loaded["telemetry_backend"] == "gateway"


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


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_storage_counter_is_repaired(isolated_omm_home, invalid):
    config.CONFIG_PATH.write_text(json.dumps({"storage_saved_bytes": invalid}))

    assert config.load_config()["storage_saved_bytes"] == 0


@pytest.mark.parametrize("invalid", [-1, 1.5, True])
def test_storage_counter_rejects_invalid_deltas(isolated_omm_home, invalid):
    with pytest.raises(ValueError, match="non-negative integer"):
        config.add_storage_saved_bytes(invalid)
