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
from typing import Callable, Iterator

import typer
from rich.console import Console
from rich.table import Table

from omm import linker
from omm.hardware import calculate_memory_budget, scan_hardware

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
    if console.size.width >= _ASCII_ART_WIDTH:
        console.print(f"[bold cyan]{_ASCII_ART}[/bold cyan]")
    else:
        console.print("[bold cyan]omm[/bold cyan] - local LLM package manager")
    console.print("[dim]Let's get you set up.[/dim]\n")


def print_hardware_summary(console: Console) -> None:
    info = scan_hardware()
    budget = calculate_memory_budget(info)

    table = Table(title="Your machine", box=None)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    table.add_row("Safe model budget", f"{budget.model_budget_gb:.1f} GB")
    if info.gpu_name:
        table.add_row("GPU", info.gpu_name)
    console.print(table)
    console.print()


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
    circular import (see module docstring)."""
    from prompt_toolkit.keys import Keys

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    application = getattr(question, "application", None)
    key_bindings = getattr(application, "key_bindings", None)
    if key_bindings is not None:
        key_bindings.add(Keys.Escape, eager=True)(_abort)
    return question


def run_engine_checklist(console: Console) -> list[str] | None:
    """Returns the selected engine keys, `[]` if the user confirmed zero
    engines, or `None` if the user aborted (Ctrl+C/Escape) - callers must
    check `is None` explicitly rather than `or []`, since collapsing the
    two means an aborted wizard would look identical to "nothing selected"
    and get marked complete anyway."""
    import questionary

    choices = _engine_choices()
    if all(installed for _, _, installed in choices):
        console.print("[dim]All known local AI runners are already installed.[/dim]\n")
        return []

    if not _stdin_is_tty():
        console.print(
            "[red]Engine selection requires an interactive terminal. "
            "Re-run `omm setup` from a real terminal.[/red]"
        )
        raise typer.Exit(1)

    question = _add_escape_to_cancel(
        questionary.checkbox(
            "Install any local AI runners you'd like to use? (space to select, enter to confirm)",
            choices=[
                questionary.Choice(
                    title=label, value=key, disabled="installed" if installed else None
                )
                for key, label, installed in choices
            ],
            instruction="",
            validate=_build_empty_selection_validator(),
        )
    )
    with _bracket_checkbox_indicators():
        return question.ask()


def _install_selected_engines(console: Console, selected: list[str]) -> None:
    specs_by_key = {spec.key: spec for spec in linker.ENGINES}
    for key in selected:
        spec = specs_by_key[key]
        if not linker.has_automated_installer(key):
            console.print(
                f"[yellow]{spec.label} isn't auto-installable yet.[/yellow] "
                f"See {COMPATIBLE_PROGRAMS_URL}"
            )
            continue
        console.print(f"\n[bold]Installing {spec.label}...[/bold]")
        result = linker.install_engine(
            key,
            on_output=lambda line: console.print(
                line, style="dim", markup=False, highlight=False
            ),
        )
        style = "green" if result.status == "installed" else "red"
        console.print(f"[{style}]{result.message}[/{style}]")


def run_wizard(console: Console) -> None:
    print_banner(console)
    print_hardware_summary(console)
    selected = run_engine_checklist(console)
    if selected is None:
        # User cancelled (Ctrl+C/Escape) - propagate as a real abort so the
        # caller never reaches `onboarding_completed=True`; the wizard
        # should retry next time instead of being marked done.
        raise typer.Abort()
    if selected:
        _install_selected_engines(console, selected)
    console.print(
        "\n[bold green]Setup complete.[/bold green] "
        "Run `omm setting` any time to change telemetry, upload, or update-channel settings.\n"
    )
