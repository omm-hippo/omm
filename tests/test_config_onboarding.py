"""Tests for onboarding_completed config flag."""

from omm import config


def test_fresh_install_starts_onboarding_incomplete(isolated_omm_home):
    cfg = config.load_config()

    assert cfg["onboarding_completed"] is False


def test_existing_config_missing_key_defaults_to_completed(isolated_omm_home):
    config.CONFIG_PATH.write_text("{}\n")

    cfg = config.load_config()

    assert cfg["onboarding_completed"] is True


def test_existing_config_with_other_keys_defaults_to_completed(isolated_omm_home):
    config.CONFIG_PATH.write_text('{"update_channel": "beta"}\n')

    cfg = config.load_config()

    assert cfg["onboarding_completed"] is True
    assert cfg["update_channel"] == "beta"


def test_marking_onboarding_complete_persists(isolated_omm_home):
    config.load_config()

    config.update_config(onboarding_completed=True)

    assert config.load_config()["onboarding_completed"] is True


def test_default_config_includes_dark_theme_fallback():
    assert config.DEFAULT_CONFIG["theme"] == "dark"
