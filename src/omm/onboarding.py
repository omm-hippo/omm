"""First-run setup wizard: ASCII banner, hardware summary, engine checklist,
and (for now) Ollama's automated install. Every other engine links out to
the compatibility wiki behind the same _AUTOMATED_ENGINES gate, so adding
automation for one later is a one-line change here plus a new branch in
linker.install_engine() - the wizard flow itself doesn't change."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from omm import linker
from omm.hardware import calculate_memory_budget, scan_hardware

COMPATIBLE_PROGRAMS_URL = "https://github.com/omm-hippo/omm/wiki/Compatible-Programs"

_AUTOMATED_ENGINES = {"ollama"}

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


def _engine_choices() -> list[tuple[str, str]]:
    """(key, label) pairs for engines not yet detected on this machine."""
    choices = []
    for spec in linker.ENGINES:
        if linker.is_engine_installed(spec.key):
            continue
        tag = (
            "auto-install"
            if spec.key in _AUTOMATED_ENGINES
            else "not yet automated - see compatibility wiki"
        )
        choices.append((spec.key, f"{spec.label}  ({tag})"))
    return choices


def run_engine_checklist(console: Console) -> list[str]:
    import questionary

    choices = _engine_choices()
    if not choices:
        console.print("[dim]All known local AI runners are already installed.[/dim]\n")
        return []

    selected = questionary.checkbox(
        "Install any local AI runners you'd like to use? (space to select, enter to confirm)",
        choices=[questionary.Choice(title=label, value=key) for key, label in choices],
    ).ask()
    return selected or []


def _install_selected_engines(console: Console, selected: list[str]) -> None:
    specs_by_key = {spec.key: spec for spec in linker.ENGINES}
    for key in selected:
        spec = specs_by_key[key]
        if key not in _AUTOMATED_ENGINES:
            console.print(
                f"[yellow]{spec.label} isn't auto-installable yet.[/yellow] "
                f"See {COMPATIBLE_PROGRAMS_URL}"
            )
            continue
        console.print(f"\n[bold]Installing {spec.label}...[/bold]")
        result = linker.install_engine(
            key, on_output=lambda line: console.print(f"[dim]{line}[/dim]")
        )
        style = "green" if result.status == "installed" else "red"
        console.print(f"[{style}]{result.message}[/{style}]")


def run_wizard(console: Console) -> None:
    print_banner(console)
    print_hardware_summary(console)
    selected = run_engine_checklist(console)
    if selected:
        _install_selected_engines(console, selected)
    console.print(
        "\n[bold green]Setup complete.[/bold green] "
        "Run `omm setting` any time to change telemetry, upload, or update-channel settings.\n"
    )
