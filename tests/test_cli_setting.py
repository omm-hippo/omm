import subprocess

import pytest
import questionary
from typer.testing import CliRunner

from omm import catalog, cli, config

runner = CliRunner()


@pytest.fixture(autouse=True)
def _canonical_git_install(monkeypatch):
    """Keep Git-channel tests independent of whether .git is in the test image."""

    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.GIT,
    )


def test_setting_catalog_trust_saves_verified_public_key(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(catalog, "public_key_fingerprint", lambda key: "abcd1234")

    result = runner.invoke(
        cli.app,
        [
            "setting",
            "catalog-trust",
            "--manifest-url",
            "https://example.com/manifest.json",
            "--public-key",
            "key",
        ],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["catalog_manifest_url"] == "https://example.com/manifest.json"
    assert saved["catalog_public_key"] == "key"


def test_setting_upload_enable_migrates_legacy_endpoint_to_gateway(isolated_omm_home):
    """omm-hippo/omm#133 closed the direct RTDB path - a config still
    pointed at it must move to the gateway rather than send doomed writes."""
    config.update_config(telemetry_endpoint=config.LEGACY_FIREBASE_ENDPOINT, telemetry_backend="firebase_legacy")

    result = runner.invoke(cli.app, ["setting", "upload", "--enable"])

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_endpoint"] == config.TELEMETRY_GATEWAY_ENDPOINT
    assert saved["telemetry_send_policy"] == "always"


def test_setting_upload_requires_explicit_endpoint_before_opt_in_once_cleared(isolated_omm_home):
    runner.invoke(cli.app, ["setting", "telemetry", "--endpoint", "none"])

    result = runner.invoke(cli.app, ["setting", "upload", "--enable"])

    assert result.exit_code == 1
    assert config.load_config()["telemetry_send_policy"] == "ask"


def test_setting_telemetry_accepts_local_self_hosted_endpoint(isolated_omm_home):
    result = runner.invoke(
        cli.app,
        ["setting", "telemetry", "--endpoint", "http://127.0.0.1:8000/v1/benchmarks"],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_backend"] == "self_hosted"


def test_setting_catalog_status_shows_configured_state(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "catalog-status"])

    assert result.exit_code == 0, result.stdout
    assert "Recommendation catalog" in result.stdout


def test_setting_catalog_rollback_reports_error_with_no_snapshots(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "catalog-rollback"])

    assert result.exit_code == 1
    assert "Catalog rollback failed" in result.stderr


def test_setting_version_defaults_to_stable_without_flags(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, result.stdout
    assert "stable" in result.stdout.lower()
    assert "main" in result.stdout.lower()


def test_setting_version_rejects_both_flags(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "version", "--stable", "--beta"])

    assert result.exit_code == 1
    assert "only one" in result.stderr.lower()


def test_setting_version_switch_to_beta_runs_perform_update_and_saves_config(isolated_omm_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: calls.append(branch) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "beta_sha")
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["setting", "version", "--beta"])

    assert result.exit_code == 0, result.stdout
    assert calls == ["beta"]
    assert config.load_config()["update_channel"] == "beta"
    assert "beta" in result.stdout.lower()


def test_setting_version_beta_refuses_package_managed_install_without_migrating(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.WINGET,
    )
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: (_ for _ in ()).throw(AssertionError("must not migrate")),
    )

    result = runner.invoke(cli.app, ["setting", "version", "--beta"])

    assert result.exit_code == 1
    assert "beta channel requires a git source installation" in result.stderr.lower()
    assert "winget upgrade --id OmmHippo.OMM -e" in result.stderr
    assert "`omm setting version --beta` left the installation unchanged" in result.stderr
    assert config.load_config().get("update_channel", "stable") == "stable"


def test_setting_version_package_managed_ignores_saved_beta_on_read(
    isolated_omm_home, monkeypatch
):
    config.update_config(update_channel="beta")
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.PIPX,
    )

    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, result.stdout
    assert "stable (package-managed)" in result.stdout
    assert "beta (beta)" not in result.stdout


def test_setting_version_stable_normalizes_package_managed_beta_without_git_update(
    isolated_omm_home, monkeypatch
):
    config.update_config(update_channel="beta")
    monkeypatch.setattr(
        cli.package_metadata,
        "install_source",
        lambda: cli.package_metadata.InstallSource.PYPI,
    )
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: (_ for _ in ()).throw(AssertionError("must not run Git update")),
    )

    result = runner.invoke(cli.app, ["setting", "version", "--stable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["update_channel"] == "stable"
    assert "stable (package-managed)" in result.stdout


def test_setting_version_switch_failure_does_not_persist_channel(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_perform_update",
        lambda branch: subprocess.CompletedProcess([], 1, stdout="", stderr="offline"),
    )

    result = runner.invoke(cli.app, ["setting", "version", "--beta"])

    assert result.exit_code == 1
    assert "offline" in result.stderr
    assert config.load_config().get("update_channel", "stable") == "stable"


def test_setting_version_is_a_noop_when_already_on_requested_channel(isolated_omm_home, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_perform_update", lambda branch: calls.append(branch))

    result = runner.invoke(cli.app, ["setting", "version", "--stable"])

    assert result.exit_code == 0, result.stdout
    assert calls == []


def test_old_top_level_commands_are_removed(isolated_omm_home):
    for args in (
        ["telemetry", "--enable"],
        ["catalog-trust", "--manifest-url", "https://example.com/m.json", "--public-key", "key"],
        ["catalog-status"],
        ["catalog-rollback"],
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code != 0, f"{args} should no longer exist at top level"


def test_setting_bare_cancel_exits_cleanly(isolated_omm_home, monkeypatch):
    # questionary.select(...) is evaluated eagerly as an argument to
    # _ask_select, so it must be stubbed too - constructing a real Question
    # probes the console and blows up on Windows CI runners.
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout


def test_setting_bare_menu_can_change_catalog_trust(isolated_omm_home, monkeypatch):
    answers = iter(["catalog-status", None])
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout


def test_setting_bare_menu_declining_another_change_exits_after_one_action(
    isolated_omm_home, monkeypatch
):
    # Only one _ask_select answer is queued ("catalog-status"); if the
    # "change another setting?" confirm is not honored the loop would call
    # _ask_select again and raise StopIteration instead of exiting cleanly.
    answers = iter(["catalog-status"])
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout


def test_setting_bare_menu_back_option_exits_without_choosing(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: "back")

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout


def test_setting_bare_menu_shows_back_option_and_current_values(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_backend="self_hosted",
        telemetry_endpoint="http://127.0.0.1:8000/v1/benchmarks",
        telemetry_send_policy="always",
    )
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    labels = [choice.title for choice in captured_choices[0]]
    assert any("self_hosted" in label for label in labels)
    assert any("http://127.0.0.1:8000/v1/benchmarks" in label for label in labels)
    assert labels[-1] == "← Back"


def test_setting_bare_menu_upload_submenu_has_back_option(isolated_omm_home, monkeypatch):
    answers = iter(["upload", "back", None])
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    upload_labels = [choice.title for choice in captured_choices[1]]
    assert upload_labels[-1] == "← Back"
    assert config.load_config()["telemetry_send_policy"] == "ask"


def test_setting_bare_menu_offers_error_reports(isolated_omm_home, monkeypatch):
    """`error-reports` exists as a standalone `omm setting error-reports`
    subcommand but was missing from the interactive menu's choice list -
    only reachable if you already knew the exact subcommand name."""
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    labels = [choice.title for choice in captured_choices[0]]
    values = [choice.value for choice in captured_choices[0]]
    assert "error-reports" in values
    assert any("Error reports" in label and "never" in label for label in labels)


def test_setting_bare_menu_error_reports_submenu_saves_policy(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_endpoint="http://127.0.0.1:8000/v1/telemetry.json",
        telemetry_backend="self_hosted",
    )
    answers = iter(["error-reports", "ask", None])
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    error_reports_labels = [choice.title for choice in captured_choices[1]]
    assert error_reports_labels[-1] == "← Back"
    assert config.load_config()["error_report_send_policy"] == "ask"


def test_setting_bare_menu_version_submenu_switches_channel(isolated_omm_home, monkeypatch):
    answers = iter(["version", "beta", None])
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(
        cli, "_perform_update", lambda branch: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "beta_sha")
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    version_labels = [choice.title for choice in captured_choices[1]]
    assert version_labels[-1] == "← Back"
    assert config.load_config()["update_channel"] == "beta"


def test_setting_bare_menu_no_longer_offers_catalog_status(isolated_omm_home, monkeypatch):
    captured_choices: list = []

    def fake_select(message, choices=None, **kwargs):
        captured_choices.append(choices)
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    labels = [choice.title for choice in captured_choices[0]]
    assert not any("Catalog status" in label for label in labels)
    assert any("Catalog trust" in label for label in labels)
    assert any("Catalog rollback" in label for label in labels)


def test_setting_catalog_status_accepts_quiet_flag_after_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "catalog-status", "--quiet"])

    assert result.exit_code == 0, result.stdout


def test_setting_catalog_status_accepts_quiet_flag_before_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["--quiet", "setting", "catalog-status"])

    assert result.exit_code == 0, result.stdout


def test_setting_theme_set_saves_and_shows_table(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "theme", "--set", "high-contrast"])

    assert result.exit_code == 0, result.stdout
    assert "high-contrast" in result.stdout
    assert config.load_config()["theme"] == "high-contrast"


def test_setting_theme_rejects_unknown_name(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "theme", "--set", "purple"])

    assert result.exit_code == 1
    # err_console output lands on result.stderr, not result.stdout - see
    # tests/test_cli_help_version.py's "No such command" test for the
    # same CliRunner convention in this suite.
    assert "light, dark, high-contrast, no-color" in result.stderr


def test_setting_theme_bare_shows_current_value(isolated_omm_home):
    config.update_config(theme="dark")

    result = runner.invoke(cli.app, ["setting", "theme"])

    assert result.exit_code == 0, result.stdout
    assert "dark" in result.stdout


def test_setting_theme_bare_with_tty_previews_and_saves_the_pick(
    isolated_omm_home, monkeypatch
):
    # Anyone who upgraded into this feature already has
    # onboarding_completed=True and so never sees the wizard's picker -
    # this is their only route to the previews. The live picker itself
    # (theme.run_picker) is a real prompt_toolkit Application and is
    # covered separately in test_theme.py; here we only need to confirm
    # configure_theme wires its result through to config.
    config.update_config(theme="dark")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_pick_theme_interactively", lambda *a, **k: "high-contrast")

    result = runner.invoke(cli.app, ["setting", "theme"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["theme"] == "high-contrast"


def test_setting_theme_bare_with_tty_keeps_current_value_on_cancel(
    isolated_omm_home, monkeypatch
):
    config.update_config(theme="dark")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_pick_theme_interactively", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["setting", "theme"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["theme"] == "dark"


def test_setting_theme_bare_without_tty_shows_table_without_prompting(
    isolated_omm_home, monkeypatch
):
    config.update_config(theme="dark")

    def _must_not_prompt(*args, **kwargs):
        raise AssertionError("bare `omm setting theme` must not prompt without a TTY")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_pick_theme_interactively", _must_not_prompt)
    monkeypatch.setattr(questionary, "select", _must_not_prompt)

    result = runner.invoke(cli.app, ["setting", "theme"])

    assert result.exit_code == 0, result.stdout
    assert "Color theme" in result.stdout
    assert "dark" in result.stdout


def test_setting_theme_with_set_flag_skips_the_picker(isolated_omm_home, monkeypatch):
    # An existing install, not a genuinely fresh one: _root's onboarding
    # gate now covers every subcommand (not just the bare `omm`
    # invocation), and a truly fresh isolated_omm_home would otherwise
    # make the monkeypatched _stdin_is_tty below also let the real setup
    # wizard fire here - which then crashes in its own engine-checklist
    # step, since that step checks onboarding.py's own (unmocked) stdin
    # state, not this one.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

    def _must_not_prompt(*args, **kwargs):
        raise AssertionError("--set must not open the picker")

    monkeypatch.setattr(cli, "_pick_theme_interactively", _must_not_prompt)

    result = runner.invoke(cli.app, ["setting", "theme", "--set", "light"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["theme"] == "light"


def test_setting_bare_menu_theme_submenu_previews_and_saves(isolated_omm_home, monkeypatch):
    answers = iter(["theme", None])
    captured_picker_args: list = []

    def fake_pick(current_name, allow_back=False):
        captured_picker_args.append((current_name, allow_back))
        return "light"

    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_pick_theme_interactively", fake_pick)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    # The menu offers the same "← Back" escape hatch as onboarding, not
    # a plain non-cancellable pick.
    assert captured_picker_args == [("dark", True)]
    assert config.load_config()["theme"] == "light"


def test_setting_bare_menu_theme_submenu_back_changes_nothing(isolated_omm_home, monkeypatch):
    config.update_config(theme="dark")
    answers = iter(["theme", None])
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_pick_theme_interactively", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["theme"] == "dark"
