"""First-run setup wizard: ASCII banner, hardware summary, engine checklist,
and (for now) Ollama's automated install. Every other engine links out to
the compatibility wiki behind linker.has_automated_installer(), so adding
automation for one later is a one-line change in that function plus a new
branch in linker.install_engine() - the wizard flow itself doesn't change.

Deliberately does not import from cli.py: cli.py already imports this
module, so the reverse import would be circular. Where this module needs
the same TTY-guard/escape-to-cancel behavior cli.py's questionary prompts
get via _require_tty/_add_escape_to_cancel, it duplicates the ~10 lines
locally rather than extracting a new shared module for it."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator

import typer
from rich.console import Console
from rich.table import Table

from omm import config as config_mod
from omm import linker
from omm import theme
from omm.hardware import calculate_memory_budget, scan_hardware

if TYPE_CHECKING:
    import questionary

COMPATIBLE_PROGRAMS_URL = "https://github.com/omm-hippo/omm/wiki/Compatible-Programs"

_ASCII_ART = r"""
 ██████╗ ███╗   ███╗███╗   ███╗
██╔═══██╗████╗ ████║████╗ ████║
██║   ██║██╔████╔██║██╔████╔██║
██║   ██║██║╚██╔╝██║██║╚██╔╝██║
╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║
 ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝
""".strip("\n")

_ASCII_ART_WIDTH = max(len(line) for line in _ASCII_ART.splitlines())


def print_banner(console: Console) -> None:
    # Deliberately hardcoded, NOT a theme role: this prints before the
    # wizard's theme step, so routing it through a role would render it in
    # whatever theme happens to be left in config.json from a previous
    # install - the exact situation the design spec's "Non-goals" section
    # ("Re-theming the ASCII banner ... Left as-is") rules out. The
    # hardcoded-color regression guard in
    # tests/test_theme_no_hardcoded_colors.py exempts these two lines by
    # exact text; changing them means updating that allowlist on purpose.
    if console.size.width >= _ASCII_ART_WIDTH:
        console.print(f"[bold blue]{_ASCII_ART}[/bold blue]")
    else:
        console.print("[bold blue]omm[/bold blue] - local LLM package manager")
    console.print("[muted]Let's get you set up.[/muted]\n")


def print_hardware_summary(console: Console) -> None:
    info = scan_hardware()
    budget = calculate_memory_budget(info)

    table = Table(title="Your machine", box=None, title_style="muted", header_style="heading")
    table.add_column("Field", style="label")
    table.add_column("Value", style="value")
    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    # Same total-RAM-based figure `omm recommend` shows as MODEL MEMORY, so
    # the two screens agree. The live number (free RAM right now minus a
    # reserve) used to be shown here alone, which on a busy machine read
    # as "0.3 GB" next to recommend's "12.4 GB" - confusing, not wrong.
    table.add_row("Model budget", f"{budget.install_budget_gb:.1f} GB")
    if budget.constrained_by_live_usage:
        table.add_row(
            "Free right now",
            f"{budget.model_budget_gb:.1f} GB (close other apps before running big models)",
        )
    if info.gpu_name:
        table.add_row("GPU", info.gpu_name)
    home = config_mod.OMM_HOME
    free_gb = _free_gb(home)
    if free_gb is not None:
        table.add_row("omm home", f"{home}  ({free_gb:.1f} GB free)")
    console.print(table)
    console.print()
    if free_gb is not None and free_gb < LOW_DISK_GB:
        console.print(
            f"[warning]Only {free_gb:.1f} GB is free on the drive holding omm's home. "
            "Models and runners install there - set OMM_HOME to a roomier drive "
            "(e.g. `OMM_HOME=D:\\omm`, see README) before installing anything.[/warning]\n"
        )


LOW_DISK_GB = 10.0


def _free_gb(path) -> float | None:
    import shutil

    try:
        return shutil.disk_usage(linker.disk_usage_path(path)).free / 1024**3
    except OSError:
        return None


def _engine_choices() -> list[tuple[str, str, bool]]:
    """(key, label, already_installed) for every known engine - installed
    ones are still listed (as a non-selectable "installed" entry) rather
    than disappearing from the screen, so the wizard shows what it already
    detected instead of only what's missing."""
    return [
        (spec.key, spec.label, linker.is_engine_installed(spec.key))
        for spec in linker.ENGINES
    ]


def _build_empty_selection_validator() -> Callable[[list[str]], bool | str]:
    """A bare Enter with nothing checked is easy to hit by accident (it's
    the same key that confirms a selection). The first such submission
    shows a warning instead of silently finishing the wizard; a second
    Enter with still nothing checked is treated as a deliberate "install
    nothing" and allowed through."""
    warned = {"once": False}

    def _validate(selected: list[str]) -> bool | str:
        if selected:
            return True
        if warned["once"]:
            return True
        warned["once"] = True
        return (
            "Nothing selected - press Enter again to continue without "
            "installing anything, or Space to select an engine."
        )

    return _validate


@contextmanager
def _bracket_checkbox_indicators() -> Iterator[None]:
    """Swap questionary's default checked/unchecked glyphs (`●`/`○`) for
    `[*]`/`[ ]` for the duration of one checkbox prompt.

    questionary hardcodes these as module-level constants rather than a
    per-call parameter, and `questionary.prompts.common` binds them via
    `from questionary.constants import ...` at import time - patching
    `questionary.constants` itself has no effect on rendering, since that
    module's own copy of the names is what the render function reads.
    Scoped to just this call (restored in `finally`) so other checkbox
    prompts elsewhere in the app (e.g. `omm import`'s model picker) keep
    the default look unless changed separately."""
    import questionary.prompts.common as qcommon

    original_selected = qcommon.INDICATOR_SELECTED
    original_unselected = qcommon.INDICATOR_UNSELECTED
    qcommon.INDICATOR_SELECTED = "[*]"
    qcommon.INDICATOR_UNSELECTED = "[ ]"
    try:
        yield
    finally:
        qcommon.INDICATOR_SELECTED = original_selected
        qcommon.INDICATOR_UNSELECTED = original_unselected


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _add_escape_to_cancel(question: questionary.Question) -> questionary.Question:
    """questionary only aborts on Ctrl+C/Ctrl+Q by default; make Escape do
    the same so `.ask()` returns None instead of requiring Ctrl+C. Mirrors
    cli._add_escape_to_cancel - duplicated (not imported) to avoid a
    circular import (see module docstring).

    `application.key_bindings` is a `_MergedKeyBindings` (prompt_toolkit
    merges questionary's bindings with the default ones), which has no
    `.add` - it must be extended via `merge_key_bindings`, not mutated."""
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.keys import Keys

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    application = getattr(question, "application", None)
    key_bindings = getattr(application, "key_bindings", None)
    if key_bindings is not None:
        escape_binding = KeyBindings()
        escape_binding.add(Keys.Escape, eager=True)(_abort)
        application.key_bindings = merge_key_bindings([key_bindings, escape_binding])
    return question


def run_theme_step(console: Console) -> str:
    """Returns the theme name in effect after this step - either the
    user's explicit pick, or the detected guess if stdin isn't a TTY or
    the prompt was cancelled. Unlike `run_engine_checklist`, a cancel
    here is not treated as aborting the whole wizard - nothing
    destructive has happened yet, so falling back to the guess is safe."""
    recommended = theme.detect_recommended()
    default = recommended or "dark"

    if not _stdin_is_tty():
        config_mod.update_config(theme=default)
        theme.apply_theme_to_console(console, default)
        return default

    label = "recommended" if recommended else None
    chosen = theme.run_picker(default, current_label=label) or default
    config_mod.update_config(theme=chosen)
    theme.apply_theme_to_console(console, chosen)
    return chosen


def run_engine_checklist(console: Console) -> list[str] | None:
    """Returns the selected engine keys, `[]` if the user confirmed zero
    engines, or `None` if the user aborted (Ctrl+C/Escape) - callers must
    check `is None` explicitly rather than `or []`, since collapsing the
    two means an aborted wizard would look identical to "nothing selected"
    and get marked complete anyway."""
    import questionary

    choices = _engine_choices()
    if all(installed for _, _, installed in choices):
        console.print("[muted]All known local AI runners are already installed.[/muted]\n")
        return []

    if not _stdin_is_tty():
        console.print(
            "[error]Engine selection requires an interactive terminal. "
            "Re-run this command from a real terminal.[/error]"
        )
        raise typer.Exit(1)

    question = _add_escape_to_cancel(
        questionary.checkbox(
            "Install any local AI runners you'd like to use? (space to select, enter to confirm)",
            choices=[
                questionary.Choice(
                    title=label if linker.has_automated_installer(key) else f"{label} (manual install)",
                    value=key,
                    disabled="installed" if installed else None,
                )
                for key, label, installed in choices
            ],
            instruction="",
            validate=_build_empty_selection_validator(),
            # prompt_toolkit's own base style hardcodes ("selected", "reverse")
            # (checked rows render in reverse video); questionary's default
            # style never overrides it since an empty class rule doesn't
            # reset an inherited attribute. Cancel it explicitly - the
            # `[*]` indicator already marks a checked row, reverse video
            # on top of that is redundant.
            style=questionary.Style([("selected", "noreverse")]),
        )
    )
    with _bracket_checkbox_indicators():
        return question.ask()


def install_selected_engines(console: Console, selected: list[str]) -> bool:
    """Install automatable selections and report whether they succeeded.

    Manual-only selections are guidance, not failed automated attempts. A real
    installer failure returns False so callers do not print "Setup complete"
    or exit successfully after the selected runner was not installed.
    """
    specs_by_key = {spec.key: spec for spec in linker.ENGINES}
    succeeded = True
    for key in selected:
        spec = specs_by_key[key]
        if not linker.has_automated_installer(key):
            console.print(
                f"[warning]{spec.label} isn't auto-installable yet.[/warning] "
                f"Install it yourself, then re-run `omm setup` or `omm link`. "
                f"See {COMPATIBLE_PROGRAMS_URL}"
            )
            continue
        console.print(f"\n[bold]Installing {spec.label}...[/bold]")
        result = linker.install_engine(
            key,
            on_output=lambda line: console.print(
                line, style="muted", markup=False, highlight=False
            ),
        )
        style = "success" if result.status == "installed" else "error"
        console.print(result.message, style=style, markup=False, highlight=False)
        if result.status != "installed":
            succeeded = False
    return succeeded


def run_completion_step(console: Console) -> None:
    """Offers to enable shell tab-completion via typer's built-in
    `--install-completion` machinery - the same engine behind the `omm
    --install-completion` command already documented in README.md, just
    surfaced here instead of requiring users to know it exists. Best-effort:
    any failure must not abort the wizard, since engines are already
    installed by this point."""
    from typer import _completion_shared

    shell = _completion_shared._get_shell_name()
    if shell not in ("bash", "zsh", "fish") or not _stdin_is_tty():
        console.print(
            "[muted]Enable tab-completion for install/remove any time: "
            "`omm --install-completion`.[/muted]\n"
        )
        return

    import questionary

    question = _add_escape_to_cancel(
        questionary.confirm(f"Enable tab-completion for omm in {shell} now?", default=True)
    )
    if not question.ask():
        return

    try:
        _, path = _completion_shared.install(shell=shell, prog_name="omm")
    except Exception:
        console.print(
            "[warning]Couldn't enable tab-completion automatically.[/warning] "
            "Run `omm --install-completion` to set it up manually.\n"
        )
        return

    console.print(
        f"[success]Tab-completion installed to {path}.[/success] "
        "Restart your shell (or open a new terminal) to use it.\n"
    )


def run_wizard(console: Console) -> None:
    print_banner(console)
    run_theme_step(console)
    print_hardware_summary(console)
    selected = run_engine_checklist(console)
    if selected is None:
        # User cancelled (Ctrl+C/Escape) - propagate as a real abort so the
        # caller never reaches `onboarding_completed=True`; the wizard
        # should retry next time instead of being marked done.
        raise typer.Abort()
    if selected:
        if not install_selected_engines(console, selected):
            console.print(
                "[error]Setup stopped because a selected runner was not installed. "
                "Fix the installer error above, then run `omm setup` again.[/error]"
            )
            raise typer.Exit(1)
    run_completion_step(console)
    console.print(
        "\n[success]Setup complete.[/success] "
        "Run `omm setting` any time to change telemetry, upload, or update-channel settings.\n"
    )
    console.print(
        "[accent]Next:[/accent] `omm recommend` picks a model that fits this PC and installs it, "
        "then `omm run` starts chatting with it.\n"
    )
    console.print(
        "[muted]Error reports are off unless you turn them on: "
        "`omm setting error-reports --ask` (see docs/error-reports.md).[/muted]\n"
    )
