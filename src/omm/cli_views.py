"""Terminal presentation only: no scans, state mutation, or runtime requests."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text


def table(*args, **kwargs) -> Table:
    kwargs.setdefault("border_style", "rule")
    kwargs.setdefault("header_style", "heading")
    kwargs.setdefault("title_style", "heading")
    return Table(*args, **kwargs)


def print_scan(console: Console, *, info, budget, hub_storage_gb: float,
               storage_saved_gb: float, engine_labels: list[str], registry: dict,
               external: list, shorten_path, details: bool = False) -> None:
    resources = table(title="omm resources", box=None)
    resources.add_column("Resource", style="label")
    resources.add_column("Available / used", style="value")
    resources.add_row("RAM", f"{info.ram_available_gb:.1f} GB available / {info.ram_total_gb:.1f} GB total")
    resources.add_row("Safe model budget now", f"{budget.model_budget_gb:.1f} GB")
    resources.add_row("Reserved for apps/OS", f"{budget.ram_safety_reserve_gb:.1f} GB+")
    if info.unified_memory:
        resources.add_row("Memory type", "Unified (shared RAM and GPU memory)")
    elif info.vram_total_gb is not None:
        free = f"{info.vram_free_gb:.1f}" if info.vram_free_gb is not None else "unknown"
        resources.add_row("VRAM", f"{free} GB available / {info.vram_total_gb:.1f} GB total")
    elif info.gpu_name:
        resources.add_row("VRAM", "Shared or unavailable from the OS")
    resources.add_row("omm hub storage", f"{hub_storage_gb:.1f} GB")
    resources.add_row("Saved via omm import", f"{storage_saved_gb:.1f} GB")
    if details:
        resources.add_row("OS", f"{info.os_name} {info.os_version}")
        resources.add_row("CPU", info.cpu)
        resources.add_row("GPU", info.gpu_name or "None detected")
    console.print(resources)
    console.print()
    engines = Text("Local AI runners: ", style="heading")
    engines.append(" · ".join(engine_labels) or "None installed", style="value")
    console.print(engines)  # Rich wraps to the actual terminal width.
    console.print()
    models = table(title="Local AI models", box=None)
    models.add_column("Model", style="accent", overflow="fold", ratio=3)
    models.add_column("Location", style="value", overflow="fold", ratio=2)
    models.add_column("Engine(s)", style="value", overflow="fold", ratio=1)
    models.add_column("Managed by omm", style="label")
    for filename, entry in registry.items():
        linked = [name for name, on in entry.get("linked", {}).items() if on]
        models.add_row(filename, "(omm hub)", ", ".join(linked) or "-", "yes")
    for item in external:
        models.add_row(item.display_name, shorten_path(item.path), item.engine, "no")
    console.print(models)
