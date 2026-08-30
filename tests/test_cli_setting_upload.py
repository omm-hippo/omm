from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_upload_bare_lists_three_policies(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "upload"])
    assert r.exit_code == 0
    out = r.output.lower()
    assert "benchmark" in out and "usage" in out and "crash" in out


def test_upload_usage_enable_disable(isolated_omm_home):
    assert runner.invoke(cli.app, ["setting", "upload", "usage", "--enable"]).exit_code == 0
    assert config.load_config()["usage_stats_policy"] == "enabled"
    assert runner.invoke(cli.app, ["setting", "upload", "usage", "--disable"]).exit_code == 0
    assert config.load_config()["usage_stats_policy"] == "never"


def test_upload_usage_bare_shows_dry_run_payload(isolated_omm_home):
    runner.invoke(cli.app, ["setting", "upload", "usage", "--enable"])
    r = runner.invoke(cli.app, ["setting", "upload", "usage"])
    assert r.exit_code == 0
    assert "install id" in r.output.lower()
    assert "ram_gb_bucket" in r.output


def test_upload_usage_reset_id_changes_it(isolated_omm_home):
    first = config.client_id()
    r = runner.invoke(cli.app, ["setting", "upload", "usage", "--reset-id"])
    assert r.exit_code == 0
    assert config.client_id() != first


def test_old_error_reports_command_is_gone(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "error-reports", "--disable"])
    assert r.exit_code != 0


def test_old_flat_upload_leaf_is_gone(isolated_omm_home):
    # `omm setting upload --enable` used to be the benchmark toggle; now it
    # is a group and the flag belongs to the `benchmark` subcommand.
    r = runner.invoke(cli.app, ["setting", "upload", "--enable"])
    assert r.exit_code != 0


def test_upload_crash_still_works(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "upload", "crash", "--disable"])
    assert r.exit_code == 0
    assert config.load_config()["error_report_send_policy"] == "never"


def test_upload_benchmark_still_works(isolated_omm_home):
    r = runner.invoke(cli.app, ["setting", "upload", "benchmark", "--disable"])
    assert r.exit_code == 0
    assert config.load_config()["telemetry_send_policy"] == "never"


def _scripted_selects(monkeypatch, answers):
    """Feed cli._ask_select a fixed sequence of answers (one per call)."""
    pending = list(answers)
    monkeypatch.setattr(cli, "_ask_select", lambda question: pending.pop(0))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    # patching _stdin_is_tty would otherwise trip the first-run wizard
    monkeypatch.setattr(cli, "_maybe_run_onboarding", lambda ctx: None)


def test_upload_bare_opens_picker_on_a_terminal(isolated_omm_home, monkeypatch):
    # channel -> action -> back out of the picker loop
    _scripted_selects(monkeypatch, ["crash", "disable", "back"])
    r = runner.invoke(cli.app, ["setting", "upload"])
    assert r.exit_code == 0
    assert config.load_config()["error_report_send_policy"] == "never"


def test_setting_menu_upload_entry_reaches_usage_and_crash(isolated_omm_home, monkeypatch):
    _scripted_selects(monkeypatch, ["upload", "usage", "enable", "back"])
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    r = runner.invoke(cli.app, ["setting"])
    assert r.exit_code == 0
    assert config.load_config()["usage_stats_policy"] == "enabled"
    # the picker prints the policy summary on this path too, not just on bare
    # `omm setting upload`
    assert "Outbound data" in r.output


def test_picker_survives_a_configure_error(isolated_omm_home, monkeypatch):
    # "always send benchmark" with no telemetry endpoint makes
    # configure_upload_benchmark raise typer.Exit; the picker must catch it
    # and keep going, not kill the whole session.
    runner.invoke(cli.app, ["setting", "telemetry", "--endpoint", "none"])
    assert not config.load_config().get("telemetry_endpoint")
    _scripted_selects(monkeypatch, ["benchmark", "enable", "back"])
    r = runner.invoke(cli.app, ["setting", "upload"])
    assert r.exit_code == 0
    assert config.load_config().get("telemetry_send_policy", "ask") != "always"
