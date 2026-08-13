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
    assert " rm " not in result.stdout.lower().replace("\n", " ")
