"""`_maybe_prompt_data_sharing_after_update` — the consent shown once after
an `omm update` that crossed the version introducing usage stats.

Tested in isolation from `update()` itself: `tests/test_cli_update.py`
exercises real SRC_DIR git/rmtree paths and must not be extended lightly.
"""

from omm import cli, config, onboarding


def test_prompts_when_never_asked_and_tty(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    called = []
    monkeypatch.setattr(onboarding, "run_data_sharing_step", lambda c: called.append(c))
    cli._maybe_prompt_data_sharing_after_update()
    assert called


def test_silent_when_not_a_tty(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    called = []
    monkeypatch.setattr(onboarding, "run_data_sharing_step", lambda c: called.append(c))
    cli._maybe_prompt_data_sharing_after_update()
    assert not called


def test_silent_when_already_answered(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    called = []
    monkeypatch.setattr(onboarding, "run_data_sharing_step", lambda c: called.append(c))
    for value in ("enabled", "never"):
        config.update_config(usage_stats_policy=value)
        cli._maybe_prompt_data_sharing_after_update()
    assert not called
