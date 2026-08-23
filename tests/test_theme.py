import io
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from omm import theme


def _fake_cli_context(no_color: bool):
    """A Click context carrying a `GlobalOptions`-shaped `obj.no_color`,
    built the same way `apply_theme_to_console`'s own context lookup
    resolves it (`typer._click`'s fork of Click when present, real Click
    otherwise - see `theme._cli_no_color_flag_active`'s docstring)."""
    try:
        from typer._click import Context as _Context
    except ImportError:
        from click import Context as _Context
    import click as _click

    class _Opts:
        pass

    opts = _Opts()
    opts.no_color = no_color
    return _Context(_click.Command("omm"), obj=opts)


def test_theme_names_are_the_four_fixed_presets():
    assert theme.THEME_NAMES == ("light", "dark", "high-contrast", "no-color")


@pytest.mark.parametrize("name", theme.THEME_NAMES)
def test_build_rich_theme_defines_every_role_and_its_bold_variant(name):
    rich_theme = theme.build_rich_theme(name)
    for role in theme.ROLES:
        assert role in rich_theme.styles
        assert f"bold {role}" in rich_theme.styles
        assert rich_theme.styles[f"bold {role}"].bold is True


def test_build_rich_theme_registers_real_styles_for_no_color():
    """`no-color` strips color via `Console.no_color`, not by leaving the
    roles unregistered - an unregistered role name makes every
    `style="accent"` call site raise `MissingStyle`."""
    styles = theme.build_rich_theme("no-color").styles
    assert styles["accent"] == theme.build_rich_theme("light").styles["accent"]


def test_build_rich_theme_falls_back_to_a_known_preset_for_unknown_names():
    unknown = theme.build_rich_theme("purple").styles
    light = theme.build_rich_theme("light").styles
    assert {role: unknown[role] for role in theme.ROLES} == {
        role: light[role] for role in theme.ROLES
    }


def test_light_preset_is_legible_on_a_light_background():
    """`warning`/`value` used to hardcode "yellow"/"white" from the
    pre-theming code, which are both near-invisible on an actual light
    terminal background - see the module docstring."""
    styles = theme.build_rich_theme("light").styles
    assert styles["error"].bold is True
    assert styles["warning"].color is not None
    assert styles["warning"].color.name not in ("yellow", "white")
    assert styles["warning"].bold is not True
    assert styles["success"].bold is True
    # The site's pressed amber (#D89400), not the dark preset's #FFB000,
    # which washes out on white.
    assert styles["accent"].color.triplet.hex == "#d89400"
    assert styles["accent"].bold is True
    assert styles["muted"].dim is True
    assert styles["value"].color is None
    assert styles["label"].dim is True and styles["rule"].dim is True


def test_dark_preset_differs_from_light_in_accent_warning_and_value():
    light = theme.build_rich_theme("light").styles
    dark = theme.build_rich_theme("dark").styles
    for role in ("accent", "warning", "value"):
        assert dark[role] != light[role], f"{role} should differ between light and dark"
    for role in ("error", "success"):
        assert dark[role].bold == light[role].bold


def test_dark_preset_uses_the_omm_site_tokens_verbatim():
    """The website's terminal mock-ups reproduce real omm output in these
    colours (omm-site design/DIRECTION.md tokens); the real CLI must
    match them so the two don't drift apart."""
    styles = theme.build_rich_theme("dark").styles
    assert styles["accent"].color.triplet.hex == "#ffb000"
    assert styles["warning"].color.triplet.hex == "#ffb000"
    assert styles["success"].color.triplet.hex == "#5bd98a"
    assert styles["error"].color.triplet.hex == "#f2645a"
    assert styles["value"].color.triplet.hex == "#f4f4f4"
    assert styles["heading"].bold is True
    for role in ("muted", "label", "rule"):
        assert styles[role].color.triplet.hex == "#767676"


def test_high_contrast_alert_roles_use_inverse_video_blocks():
    styles = theme.build_rich_theme("high-contrast").styles
    for role in ("error", "warning", "success"):
        assert styles[role].bgcolor is not None, f"{role} should keep its background block"


def test_high_contrast_non_alert_roles_do_not_force_a_foreground_color():
    """accent/muted/value must not hardcode a color: a fixed foreground
    only reads well against one kind of background, which defeats the
    point of a preset meant to be legible on any terminal."""
    styles = theme.build_rich_theme("high-contrast").styles
    for role in ("accent", "muted", "value", "heading", "label", "rule"):
        assert styles[role].color is None, f"{role} should not force a foreground color"
        assert styles[role].bgcolor is None, f"{role} should not force a background color"
    assert styles["accent"].bold is True
    assert styles["accent"].underline is True
    assert styles["muted"].dim is True
    assert styles["value"].bold is True


def test_detect_recommended_reads_colorfgbg_light_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect_recommended() == "light"


def test_detect_recommended_reads_colorfgbg_dark_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert theme.detect_recommended() == "dark"


def test_detect_recommended_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_recommended() is None


def test_detect_recommended_returns_none_on_garbage_value(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "not-a-number")
    assert theme.detect_recommended() is None


def test_apply_theme_to_console_sets_no_color_for_no_color_preset():
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "no-color")
    assert console.no_color is True


def test_apply_theme_to_console_pushes_theme_for_named_preset():
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "dark")
    assert console.no_color is False
    console.print("[accent]hello[/accent]")
    assert "hello" in console.file.getvalue()


def test_apply_theme_to_console_resets_no_color_when_switching_away_from_no_color_theme():
    """A persisted `no-color` theme preference must not permanently latch
    `console.no_color`: switching to a colored preset in the same
    invocation (`omm setting theme --set dark`, after the root callback
    loaded a `no-color` preference) must let color back on. Only a real
    `--no-color` CLI flag should survive the switch - see the next test
    and apply_theme_to_console's docstring."""
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "no-color")
    assert console.no_color is True

    theme.apply_theme_to_console(console, "dark")

    assert console.no_color is False


def test_apply_theme_to_console_keeps_no_color_when_cli_flag_is_active():
    """`--no-color` is applied once per invocation via `ctx.obj.no_color`;
    a theme applied afterwards (`omm --no-color setting theme --set
    dark`) must not undo it, unlike a merely-persisted `no-color`
    preference (previous test)."""
    console = Console(file=io.StringIO())

    with _fake_cli_context(no_color=True):
        theme.apply_theme_to_console(console, "dark")

    assert console.no_color is True


def test_apply_theme_to_console_leaves_no_color_off_when_it_was_off():
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "high-contrast")
    assert console.no_color is False


def test_print_theme_preview_renders_all_roles_without_raising():
    console = Console(file=io.StringIO(), force_terminal=True)
    for name in theme.THEME_NAMES:
        theme.print_theme_preview(console, name)
    output = console.file.getvalue()
    for role in theme.ROLES:
        assert role in output


def test_print_theme_preview_drops_the_sample_text_wording():
    console = Console(file=io.StringIO(), force_terminal=True)
    theme.print_theme_preview(console, "dark")
    assert "sample text" not in console.file.getvalue()


@pytest.mark.parametrize("name", theme.THEME_NAMES)
def test_render_preview_ansi_contains_every_role_with_no_filler_wording(name):
    output = theme.render_preview_ansi(name)
    for role in theme.ROLES:
        assert role in output
    assert "sample text" not in output


def test_render_preview_ansi_carries_real_ansi_color_codes():
    """Not an approximation via prompt_toolkit's own style system - the
    live picker embeds this via `ANSI()`, so it must actually contain
    the theme's real SGR codes."""
    output = theme.render_preview_ansi("dark")
    assert "\x1b[" in output


class _FakeApp:
    def __init__(self):
        self.exit = MagicMock()


class _FakeEvent:
    def __init__(self):
        self.app = _FakeApp()


def _handler_for(bindings, key):
    matches = [b for b in bindings.bindings if b.keys == (key,)]
    return matches[-1].handler


def test_picker_bindings_up_and_down_move_and_wrap():
    from prompt_toolkit.keys import Keys

    options = list(theme.THEME_NAMES)
    state = {"index": 0}
    bindings = theme._build_picker_key_bindings(state, options)

    _handler_for(bindings, Keys.Up)(_FakeEvent())
    assert state["index"] == len(options) - 1  # wraps to the last option

    state["index"] = 0
    _handler_for(bindings, Keys.Down)(_FakeEvent())
    assert state["index"] == 1


def test_picker_bindings_enter_confirms_the_highlighted_option():
    from prompt_toolkit.keys import Keys

    options = list(theme.THEME_NAMES)
    state = {"index": 2}
    bindings = theme._build_picker_key_bindings(state, options)

    event = _FakeEvent()
    _handler_for(bindings, Keys.ControlM)(event)

    event.app.exit.assert_called_once_with(result=options[2])


def test_picker_bindings_escape_and_ctrl_c_cancel():
    from prompt_toolkit.keys import Keys

    options = list(theme.THEME_NAMES)
    state = {"index": 1}
    bindings = theme._build_picker_key_bindings(state, options)

    event = _FakeEvent()
    _handler_for(bindings, Keys.Escape)(event)
    event.app.exit.assert_called_once_with(result=None)

    event = _FakeEvent()
    _handler_for(bindings, Keys.ControlC)(event)
    event.app.exit.assert_called_once_with(result=None)
