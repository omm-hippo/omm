import json

from rich.console import Console
from typer.testing import CliRunner

from omm import cli
from omm import theme as theme_mod

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
    assert "this machine" in result.stdout.lower()


def test_no_color_flag_disables_ansi_codes(isolated_omm_home, monkeypatch):
    from omm import config

    # CliRunner's captured stream isn't a tty, so Rich already suppresses
    # ANSI color on it regardless of --no-color - asserting a color code
    # absent against the unforced console would pass even if --no-color
    # did nothing. Force color on first (same pattern as
    # tests/test_onboarding.py's _console() at line 11) so the
    # "stripped by --no-color" assertion actually exercises the flag.
    #
    # `setting version` rather than `scan`: it prints a Table whose first
    # column is style="accent" without walking the real filesystem, so the
    # assertions below can't be short-circuited by whatever happens to be
    # on the machine running the suite.
    #
    # `_root` re-applies the *saved* theme to `cli.console` on every
    # invocation, so the fixture console's theme argument alone doesn't
    # decide what renders - pin the config to `light` too, or the default
    # (`dark`, whose accent is bright_cyan) wins and the color code below
    # never appears.
    config.update_config(theme="light")
    monkeypatch.setattr(
        cli, "console",
        Console(
            force_terminal=True, color_system="truecolor", highlight=False,
            theme=theme_mod.build_rich_theme("light"),
        ),
    )
    monkeypatch.setattr(
        cli, "err_console",
        Console(
            stderr=True, force_terminal=True, color_system="truecolor", highlight=False,
            theme=theme_mod.build_rich_theme("light"),
        ),
    )
    from omm import registry

    (isolated_omm_home / "models").mkdir(parents=True, exist_ok=True)
    (isolated_omm_home / "models" / "model.gguf").write_bytes(b"")
    registry.save_registry({"model.gguf": {"size_bytes": 1024**3, "linked": {"ollama": True}}})

    # Rich's Console.no_color only strips color SGR codes, not every
    # escape sequence - bold/dim/reset codes (used for the table's title,
    # header row and label column) legitimately survive --no-color, so
    # this checks for the accent column's own colour specifically rather
    # than for "\x1b[" being absent entirely. `light`'s accent is
    # #d89400, which truecolor emits as the 38;2;216;148;0 SGR.
    accent_sgr = "38;2;216;148;0"
    with_color = runner.invoke(cli.app, ["list"])
    assert with_color.exit_code == 0, with_color.stdout
    assert accent_sgr in with_color.stdout

    without_color = runner.invoke(cli.app, ["--no-color", "list"])
    assert without_color.exit_code == 0, without_color.stdout
    assert accent_sgr not in without_color.stdout


def test_json_on_unsupported_command_is_rejected_before_cleanup(isolated_omm_home):
    orphan = cli.MODELS_DIR / "keep.gguf"
    orphan.write_bytes(b"must not be removed by an unsupported invocation")
    result = runner.invoke(cli.app, ["cleanup", "--json"])

    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["error"]["code"] == "unsupported_json"
    assert orphan.exists()


def test_json_on_supported_command_does_not_warn(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan", "--json"])

    assert result.exit_code == 0, result.stdout
    assert "has no effect" not in result.stderr


def test_yes_on_unsupported_command_warns_instead_of_silently_no_opping(isolated_omm_home):
    result = runner.invoke(cli.app, ["cleanup", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "--yes has no effect on `omm cleanup`" in result.stderr


def test_yes_on_supported_command_does_not_warn(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "resolve_model", lambda name: (_ for _ in ()).throw(
        cli.ModelResolutionError("nope")
    ))
    # The failure path prints install suggestions, which would fetch the
    # recommendation catalog - keep this test off the network.
    monkeypatch.setattr(cli, "_print_install_suggestions", lambda query: None)

    result = runner.invoke(cli.app, ["install", "no-such-model", "--yes"])

    assert "has no effect" not in result.stderr


def test_yes_capability_is_judged_independently_for_same_named_nested_command(
    isolated_omm_home, monkeypatch
):
    """`omm engine install` and top-level `omm install` share the bare
    Click command name "install". The capability check used to key off
    that bare name (ctx.command.name), so the two would have shared one
    _YES_CAPABLE verdict instead of being judged independently by full
    command path. `install` has a confirmation prompt to skip (warning
    should stay silent); `engine install` does not (warning should fire)."""
    from omm import onboarding

    monkeypatch.setattr(cli, "resolve_model", lambda name: (_ for _ in ()).throw(
        cli.ModelResolutionError("nope")
    ))
    # The failure path prints install suggestions, which would fetch the
    # recommendation catalog - keep this test off the network.
    monkeypatch.setattr(cli, "_print_install_suggestions", lambda query: None)
    top_level = runner.invoke(cli.app, ["install", "no-such-model", "--yes"])
    assert "has no effect" not in top_level.stderr

    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda console: [])
    nested = runner.invoke(cli.app, ["engine", "install", "--yes"])
    assert "--yes has no effect on `omm engine install`" in nested.stderr


def test_json_warning_is_not_emitted_when_prog_name_has_exe_suffix(isolated_omm_home):
    # F104: the frozen Windows build (PyInstaller) ships as omm.exe, so
    # click derives that as the program name unless main() pins it. CliRunner
    # lets a test supply prog_name directly to reproduce that without a real
    # frozen binary. Before the fix, ctx.command_path was "omm.exe scan" and
    # the hardcoded removeprefix("omm ") left it untouched, so this false
    # "--json has no effect" warning fired on every command.
    result = runner.invoke(cli.app, ["scan", "--json"], prog_name="omm.exe")

    assert "has no effect" not in result.stderr


def test_json_error_uses_plain_command_name(isolated_omm_home):
    result = runner.invoke(cli.app, ["cleanup", "--json"], prog_name="omm.exe")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["command"] == "cleanup"


def test_yes_warning_is_not_emitted_when_prog_name_has_exe_suffix(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "resolve_model", lambda name: (_ for _ in ()).throw(
        cli.ModelResolutionError("nope")
    ))
    # The failure path prints install suggestions, which would fetch the
    # recommendation catalog - keep this test off the network.
    monkeypatch.setattr(cli, "_print_install_suggestions", lambda query: None)

    result = runner.invoke(
        cli.app, ["install", "no-such-model", "--yes"], prog_name="omm.exe"
    )

    assert "--yes has no effect" not in result.stderr


def test_no_color_survives_a_theme_change_in_the_same_invocation(isolated_omm_home):
    """`--no-color` is applied by the root callback; `setting theme --set`
    then applies a preset to the same module-level console afterwards. It
    must not undo the flag (it used to reset no_color to False)."""
    result = runner.invoke(cli.app, ["--no-color", "setting", "theme", "--set", "dark"])

    assert result.exit_code == 0, result.stdout
    assert cli.console.no_color is True
    assert cli.err_console.no_color is True


def test_no_color_survives_the_wizards_theme_step(isolated_omm_home, monkeypatch):
    """Same shape as above for `omm --no-color setup`: the wizard's theme
    step applies the pick to the console mid-run. In the real CLI this
    runs inside the invocation's Click context (the same one CliRunner
    creates for the sibling test above), so `--no-color` is surfaced the
    same way here: a fake context carrying `obj.no_color = True`."""
    from omm import onboarding

    console = Console(force_terminal=True, highlight=False)
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding.theme, "run_picker", lambda *a, **k: "dark")

    try:
        from typer._click import Context as _Context
    except ImportError:
        from click import Context as _Context
    import click as _click

    class _Opts:
        no_color = True

    with _Context(_click.Command("omm"), obj=_Opts()):
        onboarding.run_theme_step(console)

    assert console.no_color is True


def test_root_callback_applies_saved_theme_to_console(isolated_omm_home):
    from omm import config

    config.update_config(theme="high-contrast")

    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, result.stdout
    assert cli.console.no_color is False
