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

# Roles, and the omm-site design tokens each preset mirrors (design/DIRECTION.md
# in omm-site): the website's terminal mock-ups are reproductions of real omm
# output, so the real thing should carry the same hierarchy - dim rules and
# labels, bright values, one amber accent, green for success.
#
#   error    term-err   #F2645A      heading  ink-0 bold  (table headers, section titles)
#   warning  term-warn  #FFB000      label    ink-3       (the "Field"/"Program" column)
#   success  term-ok    #5BD98A      rule     ink-3       (box-drawing lines)
#   accent   accent     #FFB000      muted    ink-3
#   value    ink-0      #F4F4F4
ROLES = ("error", "warning", "success", "accent", "muted", "value", "heading", "label", "rule")
THEME_NAMES = ("light", "dark", "high-contrast", "no-color")

_BASE_STYLES: dict[str, dict[str, Style]] = {
    "light": {
        "error": Style(color="#c0392b", bold=True),
        # The site's amber is a dark-background colour; on white it needs
        # the pressed variant (accent-press #D89400) to stay readable.
        "warning": Style(color="#b07500"),
        "success": Style(color="#1f8a4c", bold=True),
        "accent": Style(color="#d89400", bold=True),
        "muted": Style(dim=True),
        # No forced color: a light-background terminal's own default
        # foreground is already dark and readable, and hardcoding
        # "white" here (as the pre-theming code did) is invisible on it.
        "value": Style(),
        "heading": Style(bold=True),
        "label": Style(dim=True),
        "rule": Style(dim=True),
    },
    "dark": {
        # Hex values are the omm-site tokens verbatim; rich downgrades
        # them to the nearest 256/16-colour on terminals without truecolor.
        "error": Style(color="#f2645a", bold=True),
        "warning": Style(color="#ffb000"),
        "success": Style(color="#5bd98a", bold=True),
        "accent": Style(color="#ffb000", bold=True),
        "muted": Style(color="#767676"),
        "value": Style(color="#f4f4f4"),
        "heading": Style(color="#f4f4f4", bold=True),
        "label": Style(color="#767676"),
        "rule": Style(color="#767676"),
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
        "heading": Style(bold=True, underline=True),
        "label": Style(),
        "rule": Style(),
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


def _cli_no_color_flag_active() -> bool:
    """Best-effort read of the current invocation's `--no-color` flag
    (`cli.GlobalOptions.no_color` on Click's `ctx.obj`), without theme.py
    importing cli.py (which imports theme.py - that would be circular).
    Mirrors cli.py's own `_get_current_context` fallback: some installs
    resolve Click's context stack through a bundled `typer._click` fork
    rather than the standalone `click` package, so both are tried.
    Returns `False` outside any Click context (e.g. a test calling this
    module's functions directly) rather than raising."""
    try:
        from typer._click.globals import get_current_context
    except ImportError:
        from click.globals import get_current_context
    ctx = get_current_context(silent=True)
    if ctx is None:
        return False
    return bool(getattr(ctx.obj, "no_color", False))


def apply_theme_to_console(console: Console, name: str) -> None:
    """Sets `console.no_color` for the preset being applied, except a real
    `--no-color` CLI flag for the current invocation always wins.

    Naively setting `no_color = (name == "no-color")` would make a
    *persisted* `no-color` theme preference just as sticky as the
    `--no-color` flag itself, which is wrong: switching away from it in
    the same invocation (`omm setting theme --set dark`, after the root
    callback loaded a `no-color` preference) must let color back on for
    the rest of that invocation. Only an actual `--no-color` flag - read
    from Click's current context, the same `GlobalOptions.no_color` the
    flag populates in cli.py - is a per-invocation override that must
    outlive a later theme application (`omm --no-color setting theme
    --set dark`, `omm --no-color setup`). Switching to a colored preset
    when colour is suppressed by the flag still registers that preset's
    styles, so the choice itself takes effect next time omm runs without
    the flag."""
    console.no_color = name == "no-color" or _cli_no_color_flag_active()
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
    # render_preview_ansi rebuilds a rich Console+Theme from scratch; with
    # only THEME_NAMES worth of distinct previews (4), computing each once
    # and reusing it avoids redoing that work on every arrow-key redraw.
    preview_cache: dict[str, ANSI] = {}

    def _preview_fragments():
        name = options[state["index"]]
        if name not in THEME_NAMES:
            return ANSI("")
        if name not in preview_cache:
            preview_cache[name] = ANSI(render_preview_ansi(name))
        return preview_cache[name]

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
