import questionary
from typer.testing import CliRunner

from omm import catalog, cli, config

runner = CliRunner()


def test_setting_ui_mode_can_switch_to_detailed(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "ui", "detailed"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["ui_mode"] == "detailed"


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
        ["ui", "detailed"],
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


def test_setting_bare_menu_can_change_ui_mode(isolated_omm_home, monkeypatch):
    answers = iter(["ui", "detailed", None])
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: next(answers))

    result = runner.invoke(cli.app, ["setting"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["ui_mode"] == "detailed"
