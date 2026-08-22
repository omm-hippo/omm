"""Semantic color roles for CLI output, resolved against one of four
fixed presets so the same markup (`[error]`, `[accent]`, ...) renders
correctly regardless of the user's terminal background.

`light` and `dark` differ in every role that actually depends on
background brightness to stay legible - `accent` (plain blue is a
near-illegible navy on black), `warning` (plain yellow all but
disappears on white), and `value` (literal white text is invisible on a
light background) - not just `accent`. `error`/`success` keep the same
red/green in both, since ANSI red and green already read fine against
either extreme. `high-contrast` keeps `error`/`warning`/`success` as
inverse-video blocks (a fixed fg/bg pair is guaranteed to contrast with
itself regardless of the terminal's own colors) but deliberately does
*not* force a foreground color for `accent`/`muted`/`value`: forcing a
color there would just relocate the same background-dependent
legibility problem this preset exists to solve, so they lean on
bold/underline/dim instead and inherit whatever fg the terminal already
pairs readably with its own bg. `no-color` does its work through the
`Console.no_color` flag the `--no-color` CLI option already sets, which
strips color SGR codes (but not bold/dim/underline) at render time; it
still registers `light`'s styles so the role *names* resolve.
Registering nothing (as an earlier version did) crashes every
`style="accent"` call site with `rich.errors.MissingStyle` - markup tags
tolerate an unknown style name, `style=` kwargs do not - so every
`Table` in the CLI would traceback."""

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
        # Plain "yellow" is legible on a dark terminal but all but
        # disappears on a light/white one; this darker gold keeps the
        # same "warning" hue readable on a light background.
        "warning": Style(color="dark_goldenrod"),
        "success": Style(color="green", bold=True),
        "accent": Style(color="blue", bold=True),
        "muted": Style(dim=True),
        # No forced color: a light-background terminal's own default
        # foreground is already dark and readable, and hardcoding
        # "white" here (as the pre-theming code did) is invisible on it.
        "value": Style(),
    },
    "dark": {
        "error": Style(color="red", bold=True),
        "warning": Style(color="yellow"),
        "success": Style(color="green", bold=True),
        # Plain terminal blue reads as a near-illegible navy on a black
        # background (the classic Ubuntu-bash directory-color problem).
        "accent": Style(color="bright_cyan", bold=True),
        "muted": Style(dim=True),
        "value": Style(color="white"),
    },
    "high-contrast": {
        "error": Style(color="white", bgcolor="red", bold=True),
        "warning": Style(color="black", bgcolor="yellow", bold=True),
        "success": Style(color="black", bgcolor="green", bold=True),
        # No forced foreground here (unlike the alert roles above): a
        # fixed color only reads well against one kind of background,
        # which is exactly the problem this preset exists to avoid.
        # bold+underline stays visible against the terminal's own
        # (already-readable) default foreground/background pairing.
        "accent": Style(bold=True, underline=True),
        "muted": Style(dim=True),
        "value": Style(bold=True),
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


def detect_recommended() -> str | None:
    """Best-effort guess only - "light", "dark", or `None` if undetected,
    never raises. No OSC terminal queries (unreliable inside
    multiplexers, can hang); this only pre-selects the picker cursor,
    the user always confirms.

    Returns `None` rather than guessing when `COLORFGBG` isn't set,
    since most terminal emulators (Terminal.app, iTerm2's defaults,
    Ghostty, ...) never set it - always falling back to one theme would
    show a "(recommended)" badge on it regardless of the terminal's
    actual background, which is worse than showing no badge."""
    raw = os.environ.get("COLORFGBG", "")
    if raw:
        try:
            bg = int(raw.split(";")[-1])
        except ValueError:
            bg = None
        if bg is not None:
            return "light" if bg == 7 or 9 <= bg <= 15 else "dark"
    return None


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
    on the user's real screen. Used by the non-interactive path only -
    the interactive picker uses `render_preview_ansi` so it can redraw
    a single live block instead of listing every preset up front."""
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
        preview.print(f"  [{role}]{role}[/{role}]")


def render_preview_ansi(name: str, *, width: int = 40) -> str:
    """Same one-line-per-role preview as `print_theme_preview`, but
    returned as a raw ANSI-escaped string instead of being printed. Feed
    it to `prompt_toolkit.formatted_text.ANSI()` to embed it in the live
    picker's preview pane, which needs to redraw with a different
    preset's *real* colors every time the highlight moves rather than
    approximating them with prompt_toolkit's own style system."""
    from io import StringIO

    buf = StringIO()
    live = Console(
        file=buf,
        force_terminal=True,
        no_color=(name == "no-color"),
        theme=build_rich_theme(name),
        highlight=False,
        width=width,
    )
    for role in ROLES:
        live.print(f"[{role}]{role}[/{role}]")
    return buf.getvalue()


def _build_picker_key_bindings(state: dict, options: list[str]):
    """Up/Down move the highlight (wrapping), Enter confirms whatever is
    currently highlighted, Escape/Ctrl+C cancel. Split out from
    `run_picker` so tests can drive handlers directly with a fake event
    instead of running a real terminal app - mirrors
    `cli._build_single_key_bindings`."""
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("k")
    def _up(event) -> None:
        state["index"] = (state["index"] - 1) % len(options)

    @bindings.add("down")
    @bindings.add("j")
    def _down(event) -> None:
        state["index"] = (state["index"] + 1) % len(options)

    @bindings.add("enter")
    def _enter(event) -> None:
        event.app.exit(result=options[state["index"]])

    @bindings.add(Keys.Escape, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _cancel(event) -> None:
        event.app.exit(result=None)

    return bindings


def run_picker(current: str, *, current_label: str | None = "current", allow_back: bool = False) -> str | None:
    """Live picker: an arrow-key list of presets with a preview pane
    above it that redraws in the highlighted preset's real colors on
    every move, so you see the effect of a choice before committing to
    it instead of scrolling through every preset's block up front.
    Returns the picked name, or `None` on cancel (Escape/Ctrl+C) or
    "← Back". `current_label=None` starts the cursor on `current`
    without printing any "(...)" suffix next to it - for when `current`
    is just a fallback default, not something worth badging."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    back = "← Back"
    options = list(THEME_NAMES) + ([back] if allow_back else [])
    state = {"index": options.index(current) if current in options else 0}

    def _preview_fragments():
        name = options[state["index"]]
        if name not in THEME_NAMES:
            return ANSI("")
        return ANSI(render_preview_ansi(name))

    def _list_fragments():
        fragments = []
        for i, name in enumerate(options):
            marker = "❯ " if i == state["index"] else "  "
            suffix = f" ({current_label})" if current_label and name == current and name in THEME_NAMES else ""
            style = "reverse" if i == state["index"] else ""
            fragments.append((style, f"{marker}{name}{suffix}\n"))
        return fragments

    bindings = _build_picker_key_bindings(state, options)
    root = HSplit(
        [
            Window(
                FormattedTextControl("Preview - how each kind of omm message will look:"),
                dont_extend_height=True,
            ),
            Window(FormattedTextControl(_preview_fragments), dont_extend_height=True, always_hide_cursor=True),
            Window(height=1, char=" "),
            Window(
                FormattedTextControl("Pick a color theme for omm's output:\n"),
                dont_extend_height=True,
            ),
            Window(FormattedTextControl(_list_fragments), dont_extend_height=True, always_hide_cursor=True),
        ]
    )
    application = Application(layout=Layout(root), key_bindings=bindings, full_screen=False)
    return application.run()
