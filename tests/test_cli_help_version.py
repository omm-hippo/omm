from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_bare_omm_prints_version_only():
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("Ω omm ")
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
        assert f"setting {name}" in result.stdout, f"missing setting subcommand: {name}"


def test_help_all_is_a_compact_listing_not_a_full_flag_dump():
    # `help --all` lists command names with a one-line summary (git/docker/
    # gh `-a` style), not each command's complete --help text. Per-command
    # flags belong to `omm <command> --help`, which this points readers at.
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "--skip-unfit" not in result.stdout
    assert "--manifest-url" not in result.stdout
    assert "full option list" in result.stdout
    assert len(result.stdout.splitlines()) < 100


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


def test_help_all_lists_exit_code_contract():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "0 success, 1 failure, 2 usage error" in result.stdout


def test_help_all_shows_setting_group_description():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "View or change omm settings" in result.stdout


def test_help_accepts_global_flags_after_the_subcommand_name():
    # help_cmd was deliberately left off @global_flags at one point, so
    # `omm help --quiet`/`--json`/etc errored with "No such option" even
    # though the README promises the 4 global flags work on every command
    # (see #81).
    for flag in ("--quiet", "-q", "--json", "--yes", "-y", "--no-color"):
        result = runner.invoke(cli.app, ["help", flag])
        assert result.exit_code == 0, (flag, result.stdout, result.stderr)
