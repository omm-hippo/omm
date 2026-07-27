from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_bare_omm_prints_version_only():
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("omm ")
    assert "Commands" not in result.stdout


def test_bare_omm_shows_stable_channel_by_default(isolated_omm_home):
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert "stable" in result.stdout


def test_bare_omm_shows_beta_channel_when_selected(isolated_omm_home):
    config.update_config(update_channel="beta")

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert "beta" in result.stdout


def test_help_with_no_args_matches_dash_dash_help():
    result = runner.invoke(cli.app, ["help"])
    expected = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.stdout
    assert "COMMANDS" in result.stdout
    assert result.stdout == expected.stdout


def test_help_with_command_name_shows_that_commands_help():
    result = runner.invoke(cli.app, ["help", "install"])

    assert result.exit_code == 0, result.stdout
    assert "Download a model" in result.stdout


def test_help_with_unknown_command_errors():
    result = runner.invoke(cli.app, ["help", "no-such-command"])

    assert result.exit_code == 1
    assert "No such command" in result.stderr
