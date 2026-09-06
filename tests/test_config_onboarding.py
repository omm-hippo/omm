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


def test_first_read_preserves_settings_created_concurrently(isolated_omm_home, monkeypatch):
    from pathlib import Path

    original_exists = Path.exists
    intervened = False

    def exists_with_concurrent_writer(path):
        nonlocal intervened
        existed = original_exists(path)
        if path == config.CONFIG_PATH and not existed and not intervened:
            intervened = True
            config.update_config(default_engine="lmstudio", usage_stats_policy="never")
        return existed

    monkeypatch.setattr(Path, "exists", exists_with_concurrent_writer)
    loaded = config.load_config()

    assert loaded["default_engine"] == "lmstudio"
    assert loaded["usage_stats_policy"] == "never"
    assert config.load_config()["usage_stats_policy"] == "never"
