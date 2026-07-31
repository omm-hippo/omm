from __future__ import annotations

from pathlib import Path

from omm import config, onboarding


def _context(**changes) -> onboarding.InvocationContext:
    values = {
        "stdin_is_tty": True,
        "stdout_is_tty": True,
        "command": "list",
        "skip_onboarding": False,
        "is_completion": False,
    }
    values.update(changes)
    return onboarding.InvocationContext(**values)


def test_first_interactive_invocation_runs_setup():
    assert onboarding.should_run_onboarding(
        {"onboarding_version": 0}, _context()
    )


def test_noninteractive_completion_help_and_explicit_skip_do_not_run_setup():
    current = {"onboarding_version": 0}

    assert not onboarding.should_run_onboarding(
        current, _context(stdin_is_tty=False)
    )
    assert not onboarding.should_run_onboarding(
        current, _context(stdout_is_tty=False)
    )
    assert not onboarding.should_run_onboarding(
        current, _context(is_completion=True)
    )
    assert not onboarding.should_run_onboarding(
        current, _context(command="help")
    )
    assert not onboarding.should_run_onboarding(
        current, _context(skip_onboarding=True)
    )


def test_completed_setup_does_not_run_again_automatically():
    assert not onboarding.should_run_onboarding(
        {"onboarding_version": config.CURRENT_ONBOARDING_VERSION}, _context()
    )


def test_collect_onboarding_keeps_answers_in_memory(monkeypatch):
    answers = iter(("automatic", "never", "beta", "guided"))
    monkeypatch.setattr(
        onboarding, "_ask_choice", lambda *args, **kwargs: next(answers)
    )

    state = onboarding.collect_onboarding(
        {"telemetry_send_policy": "ask", "update_channel": "stable"},
        detected=("ollama",),
    )

    assert state == onboarding.OnboardingState(
        onboarding_version=config.CURRENT_ONBOARDING_VERSION,
        default_engine=None,
        telemetry_send_policy="never",
        update_channel="beta",
        ui_mode="guided",
    )


def test_collect_onboarding_cancel_stops_immediately(monkeypatch):
    calls = []

    def cancel(*args, **kwargs):
        calls.append(1)
        return None

    monkeypatch.setattr(onboarding, "_ask_choice", cancel)

    assert onboarding.collect_onboarding({}, detected=()) is None
    assert calls == [1]


def test_apply_onboarding_uses_one_atomic_config_update(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding.config,
        "update_config",
        lambda **changes: calls.append(changes) or changes,
    )
    state = onboarding.OnboardingState(
        onboarding_version=1,
        default_engine="ollama",
        telemetry_send_policy="always",
        update_channel="beta",
        ui_mode="guided",
    )

    saved = onboarding.apply_onboarding(state)

    assert len(calls) == 1
    assert saved == calls[0]
    assert saved["default_engine"] == "ollama"
    assert saved["telemetry_send_policy"] == "always"


def test_skip_records_decision_and_preserves_existing_preferences(isolated_omm_home):
    config.update_config(
        telemetry_send_policy="never",
        update_channel="beta",
        default_engine="lmstudio",
        ui_mode="guided",
    )

    onboarding.mark_onboarding_skipped()
    saved = config.load_config()

    assert saved["onboarding_version"] == config.CURRENT_ONBOARDING_VERSION
    assert saved["telemetry_send_policy"] == "never"
    assert saved["update_channel"] == "beta"
    assert saved["default_engine"] == "lmstudio"
    assert saved["ui_mode"] == "guided"


def test_storage_and_engine_context_shows_destination_before_policy_choice():
    lines = onboarding.context_lines(
        onboarding.StorageInfo(Path("/models"), 5 * 1024**3),
        ("ollama", "lmstudio"),
        telemetry_endpoint="https://example.com/benchmarks",
    )

    assert lines == (
        "OMM keeps GGUF model files in one hub and links them to supported local AI apps.",
        "OMM does not start those apps or upload prompts and generated text.",
        "Storage: /models (5.0 GiB available)",
        "Detected without starting apps: Ollama, LM Studio",
        "Benchmark upload destination: https://example.com/benchmarks",
    )


def test_review_snapshot_for_narrow_terminal():
    state = onboarding.OnboardingState(1, "lmstudio", "always", "beta", "guided")

    assert onboarding.review_lines(state, width=20) == (
        "Default runtime: LM",
        "  Studio",
        "Benchmark uploads:",
        "  always",
        "Update channel: beta",
        "Terminal",
        "  presentation:",
        "  guided",
    )


def test_review_snapshot_for_wide_terminal():
    state = onboarding.OnboardingState(1, "lmstudio", "always", "beta", "guided")

    assert onboarding.review_lines(state, width=100) == (
        "Default runtime: LM Studio",
        "Benchmark uploads: always",
        "Update channel: beta",
        "Terminal presentation: guided",
    )
