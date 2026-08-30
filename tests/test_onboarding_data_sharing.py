import io

from rich.console import Console

from omm import config, onboarding, theme as theme_mod


def _console():
    return Console(file=io.StringIO(), theme=theme_mod.build_rich_theme("dark"))


def test_yes_enables_both(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_confirm_data_sharing", lambda default: True)
    onboarding.run_data_sharing_step(_console())
    cfg = config.load_config()
    assert cfg["usage_stats_policy"] == "enabled"
    assert cfg["error_report_send_policy"] == "ask"


def test_no_changes_nothing_meaningful(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_confirm_data_sharing", lambda default: False)
    onboarding.run_data_sharing_step(_console())
    cfg = config.load_config()
    assert cfg.get("usage_stats_policy") is None
    assert cfg.get("error_report_send_policy") is None


def test_non_tty_changes_nothing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: False)
    onboarding.run_data_sharing_step(_console())
    assert config.load_config().get("usage_stats_policy") is None


def test_prompt_failure_changes_nothing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)

    def boom(default):
        raise RuntimeError("questionary blew up")

    monkeypatch.setattr(onboarding, "_confirm_data_sharing", boom)
    onboarding.run_data_sharing_step(_console())  # must not raise
    assert config.load_config().get("usage_stats_policy") is None


def test_consent_text_covers_every_payload_concept(isolated_omm_home):
    text = onboarding._DATA_SHARING_TEXT.lower()
    for concept in ("version", "install", "os", "cpu", "ram", "vram", "gpu", "command"):
        assert concept in text, concept
