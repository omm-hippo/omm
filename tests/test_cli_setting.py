import questionary
from typer.testing import CliRunner

from omm import catalog, cli, config

runner = CliRunner()


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


def test_setting_upload_enable_uses_default_firebase_endpoint(isolated_omm_home):
    config.update_config(telemetry_endpoint=config.LEGACY_FIREBASE_ENDPOINT, telemetry_backend="firebase_legacy")

    result = runner.invoke(cli.app, ["setting", "upload", "--enable"])

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_endpoint"] == config.LEGACY_FIREBASE_ENDPOINT
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

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    upload_labels = [choice.title for choice in captured_choices[1]]
    assert upload_labels[-1] == "← Back"
    assert config.load_config()["telemetry_send_policy"] == "ask"
