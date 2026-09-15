"""Presentation helpers for the interactive ``omm recommend`` flow."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from omm import linker, predictor, recommend_metadata, recommend_status
from omm.hardware import HardwareInfo, calculate_memory_budget
from omm.recommend_selection import model_label, quantization_label, variant_warning

ACCENT = "accent"
SUCCESS = "success"
WARNING = "warning"
MUTED = "muted"

# The arrow-key picker (SELECT_STYLE/choice_title, via questionary +
# prompt_toolkit) never touches a rich `Console`, so it can't pick up
# `--no-color` the way the static panel above it does (through
# `Console.no_color` - see theme.apply_theme_to_console). `set_no_color`
# lets `cli.py`'s `recommend` command thread that same per-invocation
# flag in here; `_colors_enabled()` is the single place everything below
# (SELECT_STYLE, _prompt_style) reads color-on/off from, so it reflects
# both the `NO_COLOR` env var and the CLI flag rather than just the env
# var.
_cli_no_color = False


def set_no_color(active: bool) -> None:
    """Call once, before invoking the interactive picker, with the
    current invocation's `--no-color` CLI flag state."""
    global _cli_no_color
    _cli_no_color = active


def _colors_enabled() -> bool:
    return not _cli_no_color and "NO_COLOR" not in os.environ


# The arrow-navigable choice list (picker chrome below, plus each row's
# badge/speed/memory colors in build_rows()/choice_title()) keeps its
# original palette - only the static hardware panel above it was retuned.
_ROW_SUCCESS = "#4ade80"
_ROW_WARNING = "#fbbf24"
_ROW_METRIC = "#60a5fa"
_ROW_SIZE = "#f472b6"
_ROW_MUTED = "#6b7280"

def __getattr__(name: str):
    # SELECT_STYLE built lazily so `import omm.recommend_ui` doesn't drag in
    # questionary (and its prompt_toolkit chain) for callers that never
    # touch the interactive recommend picker.
    if name == "SELECT_STYLE":
        from questionary import Style

        return (
            Style(
                [
                    ("qmark", "fg:#22d3ee bold"),
                    ("question", "fg:#22d3ee bold"),
                    ("pointer", "fg:#22d3ee bold"),
                    ("highlighted", "fg:#ffffff bg:#164e63 bold"),
                    ("selected", f"fg:{_ROW_SUCCESS}"),
                    ("instruction", f"fg:{_ROW_MUTED}"),
                ]
            )
            if _colors_enabled()
            else Style([])
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_CAUTION_REASON = "Specialized or uncensored variant"


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
    installation: recommend_status.InstallationStatus
    model_type: str
    type_source: str
    use_case_source: str
    features: tuple[str, ...]


def _clip(value: str, width: int) -> str:
    if cell_len(value) <= width:
        return value
    if width <= 1:
        return set_cell_size(value, max(0, width))
    return set_cell_size(value, width - 1).rstrip() + "…"


def _prompt_style(style: str) -> str:
    return style if _colors_enabled() else ""


def humanize_model_name(candidate: dict) -> str:
    """Turn a Hub/GGUF identifier into a short, human-readable model name."""
    source = str(
        candidate.get("filename")
        or candidate.get("repo_id")
        or candidate.get("name")
        or "Unknown model"
    )
    label = model_label(source)
    repository_label = model_label(str(candidate.get("repo_id") or ""))
    if label.casefold() in {"ggml model", "gguf model", "model", "weights"}:
        label = repository_label or label
    elif repository_label.casefold().startswith(label.casefold() + " "):
        # A repository-only fine-tune/decoding suffix must stay visible.
        label = repository_label
    return label or "Unknown model"


def _candidate_text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(key) or "") for key in ("name", "repo_id", "filename")
    ).lower()


def _warning(candidate: dict) -> str | None:
    return variant_warning(candidate)


def _description(candidate: dict) -> str:
    raw = str(candidate.get("description") or "").strip()
    if raw.lower() == "curated default":
        return "Curated model from OMM's default catalog."
    downloads = re.search(r"([\d,]+)\s+downloads", raw, flags=re.IGNORECASE)
    if downloads:
        source = "ModelScope" if candidate.get("provider") == "modelscope" else "Hugging Face"
        return f"Popular on {source} with {downloads.group(1)} downloads."
    return raw or "A hardware-compatible local language model."


def build_rows(
    ranked: list[tuple[dict, float | None]],
    values: list[str],
    installations: list[recommend_status.InstallationStatus] | None = None,
) -> list[RecommendationRow]:
    if len(ranked) != len(values):
        raise ValueError("ranked candidates and install refs must have the same length")
    if installations is None:
        installations = [recommend_status.NOT_INSTALLED] * len(ranked)
    if len(installations) != len(ranked):
        raise ValueError("installation status count must match recommendation count")
    rows = []
    for index, ((candidate, speed), value, installation) in enumerate(
        zip(ranked, values, installations)
    ):
        labels = recommend_metadata.classify(candidate)
        warning = _warning(candidate)
        curated = str(candidate.get("description") or "").lower() == "curated default"
        if installation.installed:
            badge, badge_style = "INSTALLED", f"fg:{_ROW_SUCCESS} bold"
        elif index == 0 and not warning:
            badge, badge_style = "BEST FIT", f"fg:{_ROW_SUCCESS} bold"
        elif warning:
            badge, badge_style = "⚠ CAUTION", f"fg:{_ROW_WARNING} bold"
        elif curated:
            badge, badge_style = "OMM PICK", f"fg:{_ROW_SUCCESS} bold"
        elif "downloads" in str(candidate.get("description") or "").lower():
            badge, badge_style = "POPULAR", f"fg:{_ROW_METRIC} bold"
        else:
            badge, badge_style = "COMPATIBLE", f"fg:{_ROW_MUTED} bold"
        rows.append(
            RecommendationRow(
                candidate=candidate,
                speed=speed,
                value=value,
                display_name=humanize_model_name(candidate),
                badge=badge,
                badge_style=badge_style,
                memory_gb=predictor.estimate_required_memory_gb(candidate),
                use_case=labels.use_case,
                description=_description(candidate),
                warning=warning,
                installation=installation,
                model_type=labels.model_type,
                type_source=labels.type_source,
                use_case_source=labels.use_case_source,
                features=labels.features,
            )
        )
    return rows


def _hardware_value(label: str, value: str) -> Text:
    text = Text()
    text.append(f"{label}  ", style=MUTED)
    text.append(value, style="bold value")
    return text


def _available_memory(info: object) -> float | None:
    if not isinstance(info, HardwareInfo):
        return None
    return calculate_memory_budget(info).install_budget_gb


def print_screen(
    console: Console,
    info: object,
    candidate_count: int,
    *,
    show_caution: bool = False,
) -> None:
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
            border_style="rule",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print(f"[bold {ACCENT}]Recommended models[/]")
    if show_caution:
        caution = Text("   ⚠ CAUTION: ", style=f"bold {WARNING}")
        caution.append(_CAUTION_REASON, style=WARNING)
        console.print(caution)
    console.print(choice_header(console.size.width))


def _choice_widths(width: int) -> tuple[int, int, int, int, int]:
    # questionary adds four cells of picker chrome before every choice. Keep
    # the choice itself within the remaining width so the final column is not
    # clipped by prompt_toolkit. Hide memory, then purpose, then type as
    # space runs out; the selected-model detail always includes both labels.
    badge, type_width = 11, 10 if width >= 48 else 0
    memory, use = (13 if width >= 88 else 0), (12 if width >= 68 else 0)
    model = max(1, min(40, width - 4 - badge - 13 - type_width - memory - use))
    return model, badge, type_width, memory, use


def choice_header(width: int) -> Text:
    model_width, badge_width, type_width, memory_width, use_width = _choice_widths(width)
    header = Text("   ")
    header.append("MODEL".ljust(model_width), style=f"bold {MUTED}")
    if type_width:
        header.append("TYPE".ljust(type_width), style=f"bold {MUTED}")
    header.append("STATUS".ljust(badge_width), style=f"bold {MUTED}")
    header.append("SPEED".ljust(13), style=f"bold {MUTED}")
    if memory_width:
        header.append("MEMORY".ljust(memory_width), style=f"bold {MUTED}")
    if use_width:
        header.append("BEST FOR".ljust(use_width), style=f"bold {MUTED}")
    return header


def choice_title(row: RecommendationRow, width: int) -> list[tuple[str, str]]:
    model_width, badge_width, type_width, memory_width, use_width = _choice_widths(width)
    speed = f"~{row.speed:.0f} tok/s" if row.speed is not None else "Rules match"
    memory = f"~{row.memory_gb:.1f} GB" if row.memory_gb is not None else "Unknown"
    parts = [
        (
            _prompt_style("bold"),
            set_cell_size(_clip(row.display_name, model_width - 1), model_width),
        ),
    ]
    if type_width:
        parts.append(("", row.model_type.ljust(type_width)))
    parts.extend([
        (_prompt_style(row.badge_style), _clip(row.badge, badge_width - 1).ljust(badge_width)),
        (_prompt_style(f"fg:{_ROW_METRIC}"), speed.ljust(13)),
    ])
    if memory_width:
        parts.append((_prompt_style(f"fg:{_ROW_SIZE}"), memory.ljust(memory_width)))
    if use_width:
        parts.append(("", _clip(row.use_case, use_width).ljust(use_width)))
    return parts


def print_detail(console: Console, info: object, row: RecommendationRow) -> None:
    status = Text()
    if row.installation.installed:
        status.append("✓  ", style=f"bold {SUCCESS}")
        if row.installation.managed_by_omm:
            if row.installation.match_kind == "model_identity":
                status.append(
                    "Same model and parameter size already installed via OMM",
                    style=SUCCESS,
                )
            else:
                status.append("Already installed via OMM", style=SUCCESS)
        else:
            labels = [
                next(
                    (spec.label for spec in linker.ENGINES if spec.key == engine),
                    engine,
                )
                for engine in row.installation.engines
            ]
            if row.installation.match_kind == "model_identity":
                status.append(
                    "Same model and parameter size already installed"
                    + (f" in {', '.join(labels)}" if labels else ""),
                    style=SUCCESS,
                )
            else:
                status.append(
                    "Already installed" + (f" in {', '.join(labels)}" if labels else ""),
                    style=SUCCESS,
                )
        if row.warning:
            status.append("\n⚠  ", style=f"bold {WARNING}")
            status.append(row.warning, style=WARNING)
    elif row.warning:
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
    provider = row.candidate.get("provider")
    source = (
        "ModelScope" if provider == "modelscope" else
        "Hugging Face" if provider == "huggingface" or row.candidate.get("repo_id") else
        "Curated catalog"
    )
    package_text = Text()
    package_text.append("Source  ", style=f"bold {MUTED}")
    package_text.append(source, style=MUTED)
    package_text.append("    Quantization  ", style=f"bold {MUTED}")
    package_text.append(quantization_label(row.candidate), style=MUTED)
    filename_text = Text()
    filename_text.append("File  ", style=f"bold {MUTED}")
    filename_text.append(str(row.candidate.get("filename") or "Unknown"), style=MUTED)

    labels = Text()
    labels.append("TYPE  ", style=f"bold {MUTED}")
    labels.append(f"{row.model_type}  ·  {row.type_source}", style="value")
    labels.append("\nBEST FOR  ", style=f"bold {MUTED}")
    labels.append(f"{row.use_case}  ·  {row.use_case_source}", style="value")
    if row.features:
        labels.append("\nDECLARED FEATURES  ", style=f"bold {MUTED}")
        labels.append(", ".join(row.features), style="value")
    labels.append(
        "\nLabels describe declared tasks; quality and runtime support are not verified."
        "\nUnknown / — means there is not enough metadata to classify the model.",
        style=MUTED,
    )

    console.print()
    console.print(
        Panel(
            Group(
                Text(row.description, style="value"),
                Text(""),
                status,
                Text(""),
                metrics,
                Text(""),
                labels,
                Text(""),
                repository_text,
                package_text,
                filename_text,
            ),
            title=Text(row.display_name, style=f"bold {ACCENT}"),
            title_align="left",
            border_style="rule",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print(
        "[muted]Predicted speed is an estimate; actual performance can vary by runtime settings.[/muted]"
    )
    if row.installation.match_kind == "model_identity":
        console.print(
            "[muted]The local runtime may use a different quantization or package of this model.[/muted]"
        )
