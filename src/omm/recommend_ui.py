"""Presentation helpers for the interactive ``omm recommend`` flow."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from questionary import Style
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from omm import predictor
from omm.hardware import HardwareInfo, calculate_memory_budget

ACCENT = "blue"
SUCCESS = "green"
WARNING = "yellow"
MUTED = "#808080"
COLORS_ENABLED = "NO_COLOR" not in os.environ

SELECT_STYLE = (
    Style(
        [
            ("qmark", "fg:#22d3ee bold"),
            ("question", "fg:#22d3ee bold"),
            ("pointer", "fg:#22d3ee bold"),
            ("highlighted", "fg:#ffffff bg:#164e63 bold"),
            ("selected", "fg:#4ade80"),
            ("instruction", "fg:#6b7280"),
        ]
    )
    if COLORS_ENABLED
    else Style([])
)

_SPECIAL_VARIANT_WORDS = (
    "abliterated",
    "heretic",
    "nsfw",
    "uncensored",
)


@dataclass(frozen=True)
class RecommendationRow:
    candidate: dict
    speed: float | None
    value: str
    display_name: str
    badge: str
    badge_style: str
    memory_gb: float | None
    use_case: str
    description: str
    warning: str | None


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1].rstrip() + "…"


def _prompt_style(style: str) -> str:
    return style if COLORS_ENABLED else ""


def humanize_model_name(candidate: dict) -> str:
    """Turn a Hub/GGUF identifier into a short, human-readable model name."""
    source = str(
        candidate.get("filename")
        or candidate.get("repo_id")
        or candidate.get("name")
        or "Unknown model"
    )
    source = source.rsplit("/", 1)[-1]
    source = re.sub(r"\.gguf$", "", source, flags=re.IGNORECASE)
    source = re.sub(r"[-_.]gguf$", "", source, flags=re.IGNORECASE)
    source = re.sub(
        r"(?i)(?:[-_.](?:UD[-_.])?(?:I?Q[1-8]|BF16|FP16|F16|FP32|F32)"
        r"(?:[-_.][A-Z0-9]+)*)$",
        "",
        source,
    )
    source = re.sub(r"[-_]+", " ", source)
    return re.sub(r"\s+", " ", source).strip() or "Unknown model"


def _candidate_text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(key) or "") for key in ("name", "repo_id", "filename")
    ).lower()


def _warning(candidate: dict) -> str | None:
    text = _candidate_text(candidate)
    if any(word in text for word in _SPECIAL_VARIANT_WORDS):
        return (
            "Specialized or uncensored variant. Review its model card and "
            "behavior before installing."
        )
    return None


def _use_case(candidate: dict) -> str:
    tokens = set(re.split(r"[^a-z0-9]+", _candidate_text(candidate)))
    if {"coder", "coding", "code"} & tokens:
        return "Coding"
    if {"reasoning", "thinking"} & tokens:
        return "Reasoning"
    if {"embed", "embedding", "embeddings"} & tokens:
        return "Embeddings"
    if {"chat", "instruct", "it"} & tokens:
        return "General chat"
    return "General purpose"


def _description(candidate: dict) -> str:
    raw = str(candidate.get("description") or "").strip()
    if raw.lower() == "curated default":
        return "Curated model from OMM's default catalog."
    downloads = re.search(r"([\d,]+)\s+downloads", raw, flags=re.IGNORECASE)
    if downloads:
        return f"Popular on Hugging Face with {downloads.group(1)} downloads."
    return raw or "A hardware-compatible local language model."


def build_rows(
    ranked: list[tuple[dict, float | None]],
    values: list[str],
) -> list[RecommendationRow]:
    rows = []
    for index, ((candidate, speed), value) in enumerate(zip(ranked, values)):
        warning = _warning(candidate)
        curated = str(candidate.get("description") or "").lower() == "curated default"
        if index == 0 and not warning:
            badge, badge_style = "BEST FIT", f"fg:{SUCCESS} bold"
        elif warning:
            badge, badge_style = "⚠ CAUTION", f"fg:{WARNING} bold"
        elif curated:
            badge, badge_style = "OMM PICK", f"fg:black bg:{SUCCESS} bold"
        elif "downloads" in str(candidate.get("description") or "").lower():
            badge, badge_style = "POPULAR", f"fg:{ACCENT}"
        else:
            badge, badge_style = "COMPATIBLE", f"fg:{MUTED} bold"
        rows.append(
            RecommendationRow(
                candidate=candidate,
                speed=speed,
                value=value,
                display_name=humanize_model_name(candidate),
                badge=badge,
                badge_style=badge_style,
                memory_gb=predictor.estimate_required_memory_gb(candidate),
                use_case=_use_case(candidate),
                description=_description(candidate),
                warning=warning,
            )
        )
    return rows


def _hardware_value(label: str, value: str) -> Text:
    text = Text()
    text.append(f"{label}  ", style=MUTED)
    text.append(value, style="bold white")
    return text


def _available_memory(info: object) -> float | None:
    if not isinstance(info, HardwareInfo):
        return None
    return calculate_memory_budget(info).install_budget_gb


def print_screen(console: Console, info: object, candidate_count: int) -> None:
    console.print(
        Text(
            f"{candidate_count} compatible model{'s' if candidate_count != 1 else ''} found",
            style=f"bold {SUCCESS}",
        )
    )
    cpu = str(getattr(info, "cpu", "") or "Unknown")
    ram = getattr(info, "ram_total_gb", None)
    gpu = str(getattr(info, "gpu_name", "") or "CPU only")
    vram = getattr(info, "vram_total_gb", None)
    available = _available_memory(info)

    ram_label = f"{ram:.1f} GB" if isinstance(ram, (int, float)) else "Unknown"
    gpu_label = gpu
    if isinstance(vram, (int, float)) and vram > 0:
        gpu_label += f"  ·  {vram:.1f} GB"
    available_label = f"{available:.1f} GB" if available is not None else "Unknown"

    hardware = Table.grid(expand=True, padding=(0, 2))
    if console.size.width >= 88:
        hardware.add_column(ratio=1)
        hardware.add_column(ratio=1)
        hardware.add_row(
            _hardware_value("CPU", _clip(cpu, 34)),
            _hardware_value("RAM", ram_label),
        )
        hardware.add_row(
            _hardware_value("GPU", _clip(gpu_label, 34)),
            _hardware_value("MODEL MEMORY", available_label),
        )
    else:
        hardware.add_column()
        hardware.add_row(_hardware_value("CPU", _clip(cpu, 42)))
        hardware.add_row(_hardware_value("RAM", ram_label))
        hardware.add_row(_hardware_value("GPU", _clip(gpu_label, 42)))
        hardware.add_row(_hardware_value("MODEL MEMORY", available_label))
    console.print(
        Panel(
            hardware,
            title=f"[bold {ACCENT}]This PC[/]",
            title_align="left",
            border_style="bright_black",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print(f"[bold {ACCENT}]Recommended models[/]")
    console.print(choice_header(console.size.width))


def _choice_widths(width: int) -> tuple[int, int, int, int]:
    if width < 84:
        return max(18, width - 35), 11, 0, 13
    model = max(24, min(40, width - 63))
    return model, 13, 13, 15


def choice_header(width: int) -> Text:
    model_width, badge_width, memory_width, use_width = _choice_widths(width)
    header = Text("   ")
    header.append("MODEL".ljust(model_width), style=f"bold {MUTED}")
    header.append("STATUS".ljust(badge_width), style=f"bold {MUTED}")
    header.append("SPEED".ljust(13), style=f"bold {MUTED}")
    if memory_width:
        header.append("MEMORY".ljust(memory_width), style=f"bold {MUTED}")
    header.append("BEST FOR".ljust(use_width), style=f"bold {MUTED}")
    return header


def choice_title(row: RecommendationRow, width: int) -> list[tuple[str, str]]:
    model_width, badge_width, memory_width, use_width = _choice_widths(width)
    speed = f"~{row.speed:.0f} tok/s" if row.speed is not None else "Rules match"
    memory = f"~{row.memory_gb:.1f} GB" if row.memory_gb is not None else "Unknown"
    parts = [
        (
            _prompt_style("bold"),
            _clip(row.display_name, model_width - 1).ljust(model_width),
        ),
        (_prompt_style(row.badge_style), _clip(row.badge, badge_width - 1).ljust(badge_width)),
        (_prompt_style(f"fg:{MUTED}"), speed.ljust(13)),
    ]
    if memory_width:
        parts.append((_prompt_style(f"fg:{MUTED}"), memory.ljust(memory_width)))
    parts.append(("", _clip(row.use_case, use_width).ljust(use_width)))
    return parts


def print_detail(console: Console, info: object, row: RecommendationRow) -> None:
    status = Text()
    if row.warning:
        status.append("⚠  ", style=f"bold {WARNING}")
        status.append(row.warning, style=WARNING)
    else:
        status.append("✓  ", style=f"bold {SUCCESS}")
        status.append("Predicted to run comfortably on this PC", style=SUCCESS)

    metrics = Table.grid(expand=True, padding=(0, 2))
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    speed = f"~{row.speed:.0f} tok/s" if row.speed is not None else "Rules match"
    memory = f"~{row.memory_gb:.1f} GB" if row.memory_gb is not None else "Unknown"
    metrics.add_row(
        _hardware_value("PREDICTED SPEED", speed),
        _hardware_value("MEMORY REQUIRED", memory),
    )

    repository = str(row.candidate.get("repo_id") or row.value)
    repository_text = Text()
    repository_text.append("Repository  ", style=f"bold {MUTED}")
    repository_text.append(repository, style=MUTED)

    console.print()
    console.print(
        Panel(
            Group(
                Text(row.description, style="white"),
                Text(""),
                status,
                Text(""),
                metrics,
                Text(""),
                repository_text,
            ),
            title=Text(row.display_name, style=f"bold {ACCENT}"),
            title_align="left",
            border_style="bright_black",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print(
        "[dim]Predicted speed is an estimate; actual performance can vary by runtime settings.[/dim]"
    )
