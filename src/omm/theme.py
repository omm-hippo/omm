"""Semantic color roles for CLI output, resolved against one of four
fixed presets so the same markup (`[error]`, `[accent]`, ...) renders
correctly regardless of the user's terminal background.

`light` reproduces the exact colors this codebase used before theming
existed (tuned against one developer's own light-beige terminal); only
`accent` changes for `dark`, since plain terminal blue is classically
low-contrast on a black background. `no-color` does its work through the
`Console.no_color` flag the `--no-color` CLI option already sets, which
strips color SGR codes at render time; it still registers `light`'s
styles so the role *names* resolve. Registering nothing (as an earlier
version did) crashes every `style="accent"` call site with
`rich.errors.MissingStyle` - markup tags tolerate an unknown style name,
`style=` kwargs do not - so every `Table` in the CLI would traceback."""

from __future__ import annotations

import os

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

ROLES = ("error", "warning", "success", "accent", "muted", "value")
THEME_NAMES = ("light", "dark", "high-contrast", "no-color")

_BASE_STYLES: dict[str, dict[str, Style]] = {
    "light": {
        "error": Style(color="red", bold=True),
        "warning": Style(color="yellow"),
        "success": Style(color="green", bold=True),
        "accent": Style(color="blue", bold=True),
        "muted": Style(dim=True),
        "value": Style(color="white"),
    },
    "dark": {
        "error": Style(color="red", bold=True),
        "warning": Style(color="yellow"),
        "success": Style(color="green", bold=True),
        # Plain terminal blue reads as a near-illegible navy on a black
        # background (the classic Ubuntu-bash directory-color problem);
        # every other role carries over unchanged from `light`.
        "accent": Style(color="bright_cyan", bold=True),
        "muted": Style(dim=True),
        "value": Style(color="white"),
    },
    "high-contrast": {
        "error": Style(color="white", bgcolor="red", bold=True),
        "warning": Style(color="black", bgcolor="yellow", bold=True),
        "success": Style(color="black", bgcolor="green", bold=True),
        "accent": Style(color="cyan", bold=True),
        "muted": Style(color="white"),
        "value": Style(color="white", bold=True),
    },
}

# `no-color` needs real Style objects registered under the role names even
# though none of their colors will survive `Console.no_color` - see the
# module docstring. `light`'s definitions are as good as any for that.
_BASE_STYLES["no-color"] = _BASE_STYLES["light"]

_FALLBACK_THEME_NAME = "light"


def build_rich_theme(name: str) -> Theme:
    """Always returns a usable `Theme`. An unrecognized name (a
    hand-edited or newer-than-this-build config.json) degrades to
    `light` rather than leaving the console themeless, which would make
    every `style="<role>"` call site raise `MissingStyle`."""
    base = _BASE_STYLES.get(name)
    if base is None:
        base = _BASE_STYLES[_FALLBACK_THEME_NAME]
    styles: dict[str, Style] = {}
    for role, style in base.items():
        styles[role] = style
        # `recommend_ui.py` and a couple of `cli.py`/`onboarding.py` sites
        # build style strings like f"bold {ACCENT}" - registering the
        # compound key lets those keep working without every call site
        # needing to know whether its role is already bold.
        styles[f"bold {role}"] = style + Style(bold=True)
    return Theme(styles)


def detect_recommended() -> str:
    """Best-effort guess only - always "light" or "dark", never raises.
    No OSC terminal queries (unreliable inside multiplexers, can hang);
    this only pre-selects the picker cursor, the user always confirms."""
    raw = os.environ.get("COLORFGBG", "")
    if raw:
        try:
            bg = int(raw.split(";")[-1])
        except ValueError:
            bg = None
        if bg is not None:
            return "light" if bg == 7 or 9 <= bg <= 15 else "dark"
    return "dark"


def apply_theme_to_console(console: Console, name: str) -> None:
    """Only ever turns `no_color` *on*, never back off: `--no-color` is a
    per-invocation override that must outlive any later theme application
    in the same process (`omm --no-color setting theme --set dark`,
    `omm --no-color setup`). Switching to a colored preset when colour is
    already suppressed still registers that preset's styles, so the
    choice takes effect the next time omm runs without the flag."""
    if name == "no-color":
        console.no_color = True
    console.push_theme(build_rich_theme(name))


def print_theme_preview(console: Console, name: str) -> None:
    """One line per role, rendered in `name`'s actual styles, sharing
    `console`'s width/file/terminal-ness so the preview shows real color
    on the user's real screen."""
    preview = Console(
        file=console.file,
        width=console.size.width,
        force_terminal=console.is_terminal,
        # Inherit an already-suppressed `no_color` (e.g. `omm --no-color
        # setting theme`) so the preview doesn't print color the rest of
        # the invocation is deliberately withholding.
        no_color=(console.no_color or name == "no-color"),
        theme=build_rich_theme(name),
        highlight=False,
    )
    preview.print(f"[bold]{name}[/bold]")
    for role in ROLES:
        preview.print(f"  [{role}]{role}[/{role}]  sample text")
