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
    assert config.load_config()["usage_stats_policy"] is None


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
