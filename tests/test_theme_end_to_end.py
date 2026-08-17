"""End-to-end guard: every saved `theme` value in config.json must let a
real, table-printing command run to completion.

The unit tests in test_theme.py only exercise `build_rich_theme`/
`apply_theme_to_console` in isolation, which is exactly how the original
`no-color` crash shipped: markup tags (`[accent]...[/accent]`) silently
resolve to a null style when the style name isn't registered, but a
`style="accent"` kwarg (as used by every `rich.table.Table` column in the
CLI) raises `rich.errors.MissingStyle` instead. Only a full CliRunner
invocation of a command that builds such a table catches it."""

import pytest
from typer.testing import CliRunner

from omm import cli, config, theme

runner = CliRunner()


@pytest.mark.parametrize("name", theme.THEME_NAMES)
def test_every_preset_lets_a_table_printing_command_run(isolated_omm_home, name):
    config.update_config(theme=name)

    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, f"{name}: {result.stdout}\n{result.exception!r}"
    assert "Update channel" in result.stdout


def test_unrecognized_theme_in_config_does_not_crash(isolated_omm_home):
    """A hand-edited (or future/rolled-back) config.json can hold a name
    this build doesn't know; it must degrade to a known preset rather
    than leaving the console themeless and blowing up on `style=`."""
    config.update_config(theme="purple")

    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, f"{result.stdout}\n{result.exception!r}"
