import io

import pytest
from rich.console import Console

from omm import theme


def test_theme_names_are_the_four_fixed_presets():
    assert theme.THEME_NAMES == ("light", "dark", "high-contrast", "no-color")


@pytest.mark.parametrize("name", ["light", "dark", "high-contrast"])
def test_build_rich_theme_defines_every_role_and_its_bold_variant(name):
    rich_theme = theme.build_rich_theme(name)
    for role in theme.ROLES:
        assert role in rich_theme.styles
        assert f"bold {role}" in rich_theme.styles
        assert rich_theme.styles[f"bold {role}"].bold is True


def test_build_rich_theme_returns_none_for_no_color():
    assert theme.build_rich_theme("no-color") is None


def test_light_preset_matches_todays_hardcoded_colors():
    styles = theme.build_rich_theme("light").styles
    assert styles["error"].color.name == "red"
    assert styles["error"].bold is True
    assert styles["warning"].color.name == "yellow"
    assert styles["warning"].bold is not True
    assert styles["success"].color.name == "green"
    assert styles["success"].bold is True
    assert styles["accent"].color.name == "blue"
    assert styles["accent"].bold is True
    assert styles["muted"].dim is True
    assert styles["value"].color.name == "white"


def test_dark_preset_only_differs_from_light_in_accent():
    light = theme.build_rich_theme("light").styles
    dark = theme.build_rich_theme("dark").styles
    for role in theme.ROLES:
        if role == "accent":
            assert dark[role].color.name != light[role].color.name
        else:
            assert dark[role].color == light[role].color
            assert dark[role].bold == light[role].bold


def test_detect_recommended_reads_colorfgbg_light_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect_recommended() == "light"


def test_detect_recommended_reads_colorfgbg_dark_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert theme.detect_recommended() == "dark"


def test_detect_recommended_defaults_to_dark_when_unset(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_recommended() == "dark"


def test_detect_recommended_defaults_to_dark_on_garbage_value(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "not-a-number")
    assert theme.detect_recommended() == "dark"


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


def test_print_theme_preview_renders_all_roles_without_raising():
    console = Console(file=io.StringIO(), force_terminal=True)
    for name in theme.THEME_NAMES:
        theme.print_theme_preview(console, name)
    output = console.file.getvalue()
    for role in theme.ROLES:
        assert role in output
