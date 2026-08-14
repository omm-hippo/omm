from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def test_rm_alias_resolves_to_uninstall(isolated_omm_home):
    result = runner.invoke(cli.app, ["rm", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Uninstall a model" in result.stdout


def test_ls_alias_resolves_to_list(isolated_omm_home):
    result = runner.invoke(cli.app, ["ls", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Show models installed via omm" in result.stdout


def test_up_alias_resolves_to_upgrade(isolated_omm_home):
    result = runner.invoke(cli.app, ["up", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Refresh an installed model" in result.stdout


def test_aliases_do_not_appear_in_help_all(isolated_omm_home):
    result = runner.invoke(cli.app, ["help", "--all"])
    assert result.exit_code == 0, result.stdout
    # Aliases are never registered as their own Click commands (see
    # _COMMAND_ALIASES / _RootHelpGroup.get_command), so `help --all`'s
    # per-command enumeration - which reads root_ctx.command.commands
    # directly - never emits a standalone entry for them. A real command's
    # own help text is allowed to *mention* its alias (e.g. `uninstall`'s
    # help says "Alias: rm"), so check for the absence of a standalone
    # usage line rather than a blunt substring match.
    for alias in ("rm", "ls", "up"):
        assert f"USAGE: omm {alias} " not in result.stdout


def test_uninstall_help_documents_its_alias(isolated_omm_home):
    # `help --all` only shows a one-line summary per command now (see
    # test_help_all_is_a_compact_listing_not_a_full_flag_dump), so the
    # alias note lives in uninstall's own help text instead.
    result = runner.invoke(cli.app, ["help", "uninstall"])
    assert result.exit_code == 0, result.stdout
    assert "Alias: rm" in result.stdout
