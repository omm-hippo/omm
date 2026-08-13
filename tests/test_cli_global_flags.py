from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def test_global_flag_works_before_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["--json", "scan"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("{")


def test_global_flag_works_after_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan", "--json"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("{")


def test_scan_without_json_prints_table(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan"])
    assert result.exit_code == 0, result.stdout
    assert "omm hardware scan" in result.stdout.lower()


def test_no_color_flag_disables_ansi_codes(isolated_omm_home):
    result = runner.invoke(cli.app, ["--no-color", "scan"])
    assert result.exit_code == 0, result.stdout
    assert "\x1b[" not in result.stdout
