from rich.console import Console
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


def test_no_color_flag_disables_ansi_codes(isolated_omm_home, monkeypatch):
    # CliRunner's captured stream isn't a tty, so Rich already suppresses
    # ANSI color on it regardless of --no-color - asserting a color code
    # absent against the unforced console would pass even if --no-color
    # did nothing. Force color on first (same pattern as
    # tests/test_onboarding.py's _console() at line 11) so the
    # "stripped by --no-color" assertion actually exercises the flag.
    #
    # Rich's Console.no_color only strips color SGR codes, not every
    # escape sequence - bold/italic/reset codes (used for the table's
    # title and header row) legitimately survive --no-color, so this
    # checks for the "Field" column's cyan color code (\x1b[36m)
    # specifically rather than for "\x1b[" being absent entirely.
    monkeypatch.setattr(cli, "console", Console(force_terminal=True))
    monkeypatch.setattr(cli, "err_console", Console(stderr=True, force_terminal=True))

    with_color = runner.invoke(cli.app, ["scan"])
    assert with_color.exit_code == 0, with_color.stdout
    assert "\x1b[36m" in with_color.stdout

    without_color = runner.invoke(cli.app, ["--no-color", "scan"])
    assert without_color.exit_code == 0, without_color.stdout
    assert "\x1b[36m" not in without_color.stdout


def test_json_on_unsupported_command_warns_instead_of_silently_no_opping(isolated_omm_home):
    # `omm autoremove --json` previously exited 0 and printed plain text -
    # a script piping that expecting JSON would get garbage silently (see
    # #81). autoremove doesn't restructure its output for --json.
    result = runner.invoke(cli.app, ["autoremove", "--json"])

    assert result.exit_code == 0, result.stdout
    assert "--json has no effect on `omm autoremove`" in result.stderr


def test_json_on_supported_command_does_not_warn(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan", "--json"])

    assert result.exit_code == 0, result.stdout
    assert "has no effect" not in result.stderr


def test_yes_on_unsupported_command_warns_instead_of_silently_no_opping(isolated_omm_home):
    result = runner.invoke(cli.app, ["autoremove", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "--yes has no effect on `omm autoremove`" in result.stderr


def test_yes_on_supported_command_does_not_warn(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "resolve_model", lambda name: (_ for _ in ()).throw(
        cli.ModelResolutionError("nope")
    ))

    result = runner.invoke(cli.app, ["install", "no-such-model", "--yes"])

    assert "has no effect" not in result.stderr
