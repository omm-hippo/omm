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
    assert "Example usage" in result.stdout
    assert result.stdout == expected.stdout


def test_help_all_lists_every_command():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "autoremove" in result.stdout


def test_help_all_expands_nested_setting_subcommands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    for name in ("telemetry", "upload", "version", "calibrate", "catalog-trust", "catalog-status", "catalog-rollback"):
        assert name in result.stdout, f"missing setting subcommand: {name}"
    # Not just names from the group's own summary table - actual per-
    # subcommand flags too, proving the renderer recursed into each
    # setting subcommand's own --help rather than stopping at `setting`.
    assert "--manifest-url" in result.stdout  # catalog-trust only
    assert "--enable" in result.stdout and "--disable" in result.stdout  # upload only


def test_help_all_shows_flags_not_just_command_names():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "--skip-unfit" in result.stdout
    assert "--json" in result.stdout


def test_help_all_excludes_hidden_commands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "_bg-version-check" not in result.stdout


def test_help_all_setting_subcommand_usage_line_is_not_double_prefixed():
    # Regression guard: the nested-group renderer once built each setting
    # subcommand's context with the already-prefixed name (e.g.
    # "setting calibrate") as a child of a context whose own info_name was
    # "setting", so Click's usage-line builder walked the parent chain and
    # duplicated the prefix into "setting setting calibrate".
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "setting setting" not in result.stdout
    assert "setting calibrate" in result.stdout


def test_help_with_command_name_shows_that_commands_help():
    result = runner.invoke(cli.app, ["help", "install"])

    assert result.exit_code == 0, result.stdout
    assert "Download a model" in result.stdout


def test_help_with_unknown_command_errors():
    result = runner.invoke(cli.app, ["help", "no-such-command"])

    assert result.exit_code == 1
    assert "No such command" in result.stderr
