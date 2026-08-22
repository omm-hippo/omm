"""`omm fit` / `omm info` memory card: one model laid over this PC's RAM.

Reproduces the memory-budget card on omm.run in the terminal - the same
four numbers `omm scan` already prints, but drawn as a bar so "does this
4.4 GB file fit next to what's already running?" is answered at a glance
instead of by subtracting scan rows in your head.

    RAM 15.5 GB · INTEL CORE ULTRA 7 155H · WINDOWS 11

                                   4.37 GB MODEL ┃
    ████████████▓▓▓▓███████████░░░░░░░░░░░░░░┊░░░░
    in use        reserved  model       free  cap

    In use by other apps                        5.7 GB
    Reserved for apps/OS                        1.6 GB+
    Safe model budget - the smaller of the two  8.2 GB
    Install cap - 80% of total RAM             12.4 GB

    ✓ Fits now - 3.8 GB to spare after runtime overhead

Pure rendering: every number comes in from hardware.calculate_memory_budget
and predictor.estimate_required_memory_gb, nothing is recomputed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from omm.hardware import RAM_MODEL_CAP_RATIO, HardwareInfo, MemoryBudget

BAR_MIN = 24
BAR_MAX = 72


@dataclass(frozen=True)
class FitVerdict:
    status: str  # "fits" | "tight" | "too_big"
    message: str
    role: str  # theme role for the line


def verdict(required_gb: float, budget: MemoryBudget) -> FitVerdict:
    """`required_gb` already includes runtime overhead (predictor's 1.2x)."""
    spare_now = budget.model_budget_gb - required_gb
    spare_cap = budget.install_budget_gb - required_gb
    if spare_now >= 0:
        return FitVerdict("fits", f"Fits now - {spare_now:.1f} GB to spare after runtime overhead", "success")
    if spare_cap >= 0:
        return FitVerdict(
            "tight",
            f"Fits this PC, but not right now - free {-spare_now:.1f} GB more "
            "(close other apps) before running it",
            "warning",
        )
    return FitVerdict(
        "too_big",
        f"Too big for this PC - needs {-spare_cap:.1f} GB more than the install cap even with everything closed",
        "error",
    )


def _bar(width: int, hw: HardwareInfo, budget: MemoryBudget, required_gb: float) -> Text:
    """Segments left→right: in use · reserved · this model · free. The
    install cap (80% of total) is a tick so the model's end can be read
    against it. Widths are proportional to total RAM; anything past the
    end of the bar (a model larger than what's left) is clipped, which
    is exactly the picture the verdict line then puts into words."""
    total = max(hw.ram_total_gb, 0.1)
    in_use = max(0.0, hw.ram_total_gb - hw.ram_available_gb)
    reserve = budget.ram_safety_reserve_gb

    def cells(gb: float) -> int:
        return int(round(width * gb / total))

    used_w = min(width, cells(in_use))
    reserve_w = min(width - used_w, cells(reserve))
    model_w = min(width - used_w - reserve_w, cells(required_gb))
    free_w = width - used_w - reserve_w - model_w
    cap_col = min(width - 1, cells(total * RAM_MODEL_CAP_RATIO))

    bar = Text()
    bar.append("█" * used_w, style="label")
    bar.append("▓" * reserve_w, style="muted")
    bar.append("█" * model_w, style="accent")
    bar.append("░" * free_w, style="rule")
    # Overlay the cap tick without disturbing segment widths.
    if 0 <= cap_col < width:
        bar.stylize("muted", cap_col, cap_col + 1)
        bar = Text.assemble(bar[:cap_col], ("┊", "muted"), bar[cap_col + 1 :])
    return bar


def _legend(width: int, hw: HardwareInfo, budget: MemoryBudget, required_gb: float) -> Text:
    total = max(hw.ram_total_gb, 0.1)
    in_use = max(0.0, hw.ram_total_gb - hw.ram_available_gb)

    def cells(gb: float) -> int:
        return int(round(width * gb / total))

    used_w = min(width, cells(in_use))
    reserve_w = min(width - used_w, cells(budget.ram_safety_reserve_gb))
    model_w = min(width - used_w - reserve_w, cells(required_gb))

    # A label is printed only when its segment is wide enough to hold it
    # whole; a truncated "rese" explains nothing. The numbers table below
    # names every segment anyway.
    legend = Text()
    for w, label, role in (
        (used_w, "in use", "label"),
        (reserve_w, "reserved", "muted"),
        (model_w, "model", "accent"),
    ):
        if w <= 0:
            continue
        legend.append((label if len(label) <= w else "").ljust(w), style=role)
    rest = width - len(legend.plain)
    if rest >= 4:
        legend.append("free".rjust(rest), style="rule")
    return legend


def _marker_line(width: int, hw: HardwareInfo, budget: MemoryBudget, required_gb: float, size_gb: float) -> Text:
    """`4.37 GB MODEL ┃` ending where the model segment ends."""
    total = max(hw.ram_total_gb, 0.1)
    in_use = max(0.0, hw.ram_total_gb - hw.ram_available_gb)
    end_gb = in_use + budget.ram_safety_reserve_gb + required_gb
    col = min(width, max(1, int(round(width * end_gb / total))))
    label = f"{size_gb:.2f} GB MODEL "
    pad = max(0, col - len(label) - 1)
    line = Text(" " * pad)
    line.append(label, style="accent")
    line.append("┃", style="accent")
    return line


def render_fit(
    *,
    hw: HardwareInfo,
    budget: MemoryBudget,
    model_label: str,
    size_gb: float,
    required_gb: float,
    width: int,
) -> RenderableType:
    """A Panel sized to `width` (the console width) holding the card."""
    inner = max(BAR_MIN, min(BAR_MAX, width - 6))
    head = Text()
    head.append(f"RAM {hw.ram_total_gb:.1f} GB", style="muted")
    head.append("  ·  ", style="rule")
    head.append(_short_cpu(hw.cpu).upper(), style="muted")
    head.append("  ·  ", style="rule")
    head.append(f"{hw.os_name} {hw.os_version}".upper(), style="muted")

    rows = Table.grid(padding=(0, 2), expand=True)
    rows.add_column(ratio=1)
    rows.add_column(justify="right", no_wrap=True)
    in_use = max(0.0, hw.ram_total_gb - hw.ram_available_gb)
    rows.add_row(Text("In use by other apps", style="value"), Text(f"{in_use:.1f} GB", style="bold value"))
    rows.add_row(
        Text("Reserved for apps/OS", style="value"),
        Text(f"{budget.ram_safety_reserve_gb:.1f} GB+", style="bold value"),
    )
    rows.add_row(
        Text("Safe model budget - the smaller of the two", style="value"),
        Text(f"{budget.model_budget_gb:.1f} GB", style="bold value"),
    )
    rows.add_row(
        Text(f"Install cap - {int(RAM_MODEL_CAP_RATIO * 100)}% of total RAM", style="muted"),
        Text(f"{budget.install_budget_gb:.1f} GB", style="muted"),
    )
    rows.add_row(
        Text(f"This model - {size_gb:.2f} GB file + runtime overhead", style="accent"),
        Text(f"{required_gb:.1f} GB", style="bold accent"),
    )

    v = verdict(required_gb, budget)
    glyph = {"fits": "✓", "tight": "!", "too_big": "✗"}[v.status]
    verdict_line = Text(f"{glyph}  {v.message}", style=v.role)

    body = Group(
        head,
        Text(""),
        _marker_line(inner, hw, budget, required_gb, size_gb),
        _bar(inner, hw, budget, required_gb),
        _legend(inner, hw, budget, required_gb),
        Text(""),
        rows,
        Text(""),
        verdict_line,
    )
    return Panel(
        body,
        title=Text(model_label, style="bold accent"),
        title_align="left",
        border_style="rule",
        box=box.ROUNDED,
        padding=(1, 2),
        width=min(width, inner + 6),
    )


def _short_cpu(cpu: str) -> str:
    """`Intel(R) Core(TM) Ultra 7 155H` → `Intel Core Ultra 7 155H`."""
    for junk in ("(R)", "(TM)", "(r)", "(tm)", " CPU", " Processor"):
        cpu = cpu.replace(junk, "")
    return " ".join(cpu.split())
