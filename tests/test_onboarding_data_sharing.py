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
    assert cfg.get("usage_stats_policy") == "never"
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


# Every payload key must have an explicit phrase promised in the
# consent text. Adding a field to usage._snapshot() without adding a
# row here (and a matching phrase in the text) fails this test.
_CONSENT_PHRASE_FOR_PAYLOAD_KEY = {
    "schema_version": None,      # internal wire version, nothing user-visible
    "client_id": "random id",
    "client_version": "omm version",
    "install_source": "install method",
    "os_name": "os",
    "os_version": "os",
    "cpu_arch": "cpu architecture",
    "ram_gb_bucket": "ram",
    "vram_gb_bucket": "vram",
    "gpu_vendor": "gpu vendor",
    "recorded_at": None,         # the batch's own timestamp
    "update_channel": None,      # stable/beta, not user data
    "commands": "which commands you ran",
    "errors": "whether they succeeded",
}


def test_consent_text_covers_every_payload_concept(isolated_omm_home, monkeypatch):
    from omm import usage

    monkeypatch.setattr(usage, "policy", lambda: "enabled")
    usage.record_run("install", "failed", "DownloadError")
    payload_keys = set(usage.build_payload())
    text = onboarding._DATA_SHARING_TEXT.lower()
    unmapped = payload_keys - set(_CONSENT_PHRASE_FOR_PAYLOAD_KEY)
    assert not unmapped, f"new payload field(s) with no consent-text mapping: {unmapped}"
    for key in payload_keys:
        phrase = _CONSENT_PHRASE_FOR_PAYLOAD_KEY[key]
        if phrase is None:
            continue
        assert phrase in text, f"{key}: consent text does not promise '{phrase}'"


def test_privacy_doc_table_lists_exactly_the_payload_fields(isolated_omm_home, monkeypatch):
    """PRIVACY.md's 'Sent - exactly these fields' table is the public
    contract; a new usage field must appear there too."""
    import re
    from pathlib import Path

    from omm import usage

    monkeypatch.setattr(usage, "policy", lambda: "enabled")
    usage.record_run("install", "failed", "DownloadError")
    doc = Path(__file__).resolve().parents[1] / "PRIVACY.md"
    documented = set(re.findall(r"^\| `([a-z_]+)`(?: / `([a-z_]+)`)?", doc.read_text(encoding="utf-8"), re.M))
    documented = {name for pair in documented for name in pair if name}
    missing = set(usage.build_payload()) - documented - {"schema_version"}
    assert not missing, f"undocumented usage fields in PRIVACY.md: {missing}"


def test_consent_text_does_not_promise_a_per_report_prompt(isolated_omm_home):
    # error_report_send_policy is set to "ask" (never "always"), whose one
    # prompt happens once per `omm contribute` run, before the whole queue
    # is sent - not once per report. The consent text must not claim
    # otherwise.
    assert "before each" not in onboarding._DATA_SHARING_TEXT.lower()


def test_cancelled_prompt_changes_nothing(isolated_omm_home, monkeypatch):
    """Escape (questionary's `.ask()` returns None) is "not now", not "never":
    nothing is recorded, so the question can be asked again later."""
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_confirm_data_sharing", lambda default: None)
    onboarding.run_data_sharing_step(_console())
    cfg = config.load_config()
    assert cfg.get("usage_stats_policy") is None
    assert cfg.get("error_report_send_policy") is None
