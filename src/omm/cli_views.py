"""Terminal presentation only: no scans, state mutation, or runtime requests."""
from __future__ import annotations

from rich.columns import Columns
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
               external: list, shorten_path, runners_note: str | None = None) -> None:
    # Readability first (#339): section titles and important names are bold
    # in the terminal's own foreground; no dim/grey on information and no
    # colour on low-value words. Hardware identity (OS/CPU/GPU) stays in
    # --json only - it does not change what fits.
    console.print(Text("This machine", style="heading"))
    resources = Table.grid(padding=(0, 2))
    resources.add_column(no_wrap=True)
    resources.add_column(overflow="fold")
    resources.add_row("RAM", f"{info.ram_available_gb:.1f} GB free of {info.ram_total_gb:.1f} GB")
    if info.unified_memory:
        resources.add_row("VRAM", "Unified with RAM")
    elif info.vram_total_gb is not None:
        free = f"{info.vram_free_gb:.1f}" if info.vram_free_gb is not None else "?"
        resources.add_row("VRAM", f"{free} GB free of {info.vram_total_gb:.1f} GB")
    elif info.gpu_name:
        resources.add_row("VRAM", "Shared or not reported by the OS")
    resources.add_row("Safe model budget",
                      f"{budget.model_budget_gb:.1f} GB now ({budget.ram_safety_reserve_gb:.1f} GB+ kept for apps/OS)")
    resources.add_row("omm hub storage",
                      f"{hub_storage_gb:.1f} GB ({storage_saved_gb:.1f} GB saved via omm import)")
    console.print(resources)
    console.print()
    console.print(Text("Local AI runners", style="heading"))
    if engine_labels:
        # Horizontal list that wraps with the terminal; a runner name is
        # never split across lines.
        console.print(Columns([Text(label, style="heading") for label in engine_labels],
                              padding=(0, 3)))
    else:
        console.print("None installed")
    if runners_note:
        console.print(runners_note)
    console.print()
    models = table(title="Local AI models", box=None, title_justify="left", pad_edge=False)
    models.add_column("Model", style="heading", overflow="fold", ratio=3)
    models.add_column("Location", overflow="fold", ratio=2)
    models.add_column("Engine(s)", overflow="fold", ratio=1)
    models.add_column("Managed by omm")
    for filename, entry in registry.items():
        linked = [name for name, on in entry.get("linked", {}).items() if on]
        models.add_row(filename, "(omm hub)", ", ".join(linked) or "-", "yes")
    for item in external:
        models.add_row(item.display_name, shorten_path(item.path), item.engine, "no")
    console.print(models)


def print_engines(console: Console, engines: list[dict], *, diagnostics: bool = False) -> None:
    view = table(title="Local AI runners", box=None)
    for name in ("Engine", "Application", "Package / version", "Local API"):
        view.add_column(name, style="value" if name != "Engine" else "heading", overflow="fold")
    for engine in engines:
        package = engine["package"]
        package_label = (f"{package['manager']} / {package['version'] or 'unknown'}"
                         if package else "Not identified")
        api = engine["api_status"]
        api_label = {"ready": "Ready", "not_checked": "Not checked",
                     "diagnostics_unavailable": "Not supported",
                     "server_unavailable": "Off or unreachable"}.get(api, api.replace("_", " "))
        view.add_row(engine["label"], "Installed" if engine["installed"] else "Not detected",
                     package_label, api_label)
        if diagnostics and engine.get("package_error"):
            console.print(engine["package_error"], markup=False)
    console.print(view)
    if diagnostics:
        for engine in engines:
            if not engine["installed"]:
                console.print(f"{engine['label']}: install with `omm engine install {engine['key']}`.", markup=False)
            elif engine["api_status"] not in {"ready", "not_checked", "diagnostics_unavailable"}:
                console.print(f"{engine['label']}: enable its local API, then retry `omm verify MODEL --engine {engine['key']}`.", markup=False)
            if not engine["package"]:
                console.print(f"{engine['label']}: package changes need an identified manager; manual options: {engine['manual_url']}", markup=False)
