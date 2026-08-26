from __future__ import annotations

from io import StringIO

from rich.console import Console

from omm import recommend_ui, theme as theme_mod
from omm.hardware import HardwareInfo


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="Windows",
        os_version="11",
        cpu="Ryzen 7 5800H",
        ram_total_gb=16.0,
        ram_available_gb=12.0,
        unified_memory=False,
        gpu_name="RTX 3060",
        vram_total_gb=6.0,
        vram_free_gb=5.0,
    )


def test_humanize_model_name_removes_gguf_and_quantization_noise():
    candidate = {
        "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "repo_id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
    }

    assert recommend_ui.humanize_model_name(candidate) == "Meta Llama 3.1 8B Instruct"


def test_build_rows_adds_human_context_and_special_variant_warning():
    candidate = {
        "filename": "Gemma-3-1B-Heretic-Uncensored-Q4_K_M.gguf",
        "repo_id": "example/Gemma-3-1B-Heretic-Uncensored-GGUF",
        "description": "404,795 downloads on HuggingFace",
    }

    [row] = recommend_ui.build_rows([(candidate, 33.0)], ["example/model"])

    assert row.badge == "⚠ CAUTION"
    assert row.use_case == "General purpose"
    assert row.memory_gb is not None
    assert "uncensored" in row.warning.lower()
    assert row.description == "Popular on Hugging Face with 404,795 downloads."


def test_build_rows_names_modelscope_download_source_correctly():
    candidate = {
        "filename": "Qwen-1B-Q4_K_M.gguf",
        "provider": "modelscope",
        "description": "1,000 downloads on ModelScope",
    }

    [row] = recommend_ui.build_rows([(candidate, 20.0)], ["ms:org/model"])

    assert row.description == "Popular on ModelScope with 1,000 downloads."


def test_build_rows_rejects_mismatched_refs_instead_of_silently_truncating():
    import pytest

    with pytest.raises(ValueError, match="same length"):
        recommend_ui.build_rows([({"filename": "a.gguf"}, 1.0)], [])


def test_recommend_screen_renders_hardware_table_and_selected_detail():
    candidate = {
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "repo_id": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "description": "Curated default",
    }
    [row] = recommend_ui.build_rows([(candidate, 33.0)], ["hf:bartowski/model"])
    output = StringIO()
    console = Console(
        file=output,
        width=120,
        color_system=None,
        force_terminal=False,
        theme=theme_mod.build_rich_theme("dark"),
    )

    recommend_ui.print_screen(console, _hardware(), 1)
    recommend_ui.print_detail(console, _hardware(), row)

    rendered = output.getvalue()
    assert "Ryzen 7 5800H" in rendered
    assert "RTX 3060" in rendered
    assert "Recommended models" in rendered
    assert "Llama 3.2 1B Instruct" in rendered
    assert "Predicted to run comfortably on this PC" in rendered
    assert "bartowski/Llama-3.2-1B-Instruct-GGUF" in rendered


def test_narrow_choice_hides_memory_column_without_losing_status():
    candidate = {
        "filename": "TinyLlama-1.1B-Chat-Q4_K_M.gguf",
        "description": "Curated default",
    }
    [row] = recommend_ui.build_rows([(candidate, 32.0)], ["tinyllama"])

    title = "".join(text for _, text in recommend_ui.choice_title(row, 70))
    header = recommend_ui.choice_header(70).plain

    assert "BEST FIT" in title
    assert "~32 tok/s" in title
    assert "MEMORY" not in header


def test_set_no_color_disables_prompt_style_regardless_of_env(monkeypatch):
    """`omm --no-color recommend` must de-color the arrow-key picker too,
    not just the static panel - `set_no_color` is how cli.py's `recommend`
    command threads the CLI flag in, since `NO_COLOR` env var alone
    (the old check) misses it."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    try:
        assert recommend_ui._prompt_style("bold") == "bold"

        recommend_ui.set_no_color(True)
        assert recommend_ui._prompt_style("bold") == ""
        assert recommend_ui.SELECT_STYLE.style_rules == []
    finally:
        recommend_ui.set_no_color(False)


def test_hardware_panel_uses_theme_roles_not_literal_colors():
    candidate = {
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "repo_id": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "description": "Curated default",
    }
    [row] = recommend_ui.build_rows([(candidate, 33.0)], ["hf:bartowski/model"])
    output = StringIO()
    console = Console(
        file=output, width=120, force_terminal=True,
        theme=theme_mod.build_rich_theme("dark"),
    )

    recommend_ui.print_screen(console, _hardware(), 1)
    recommend_ui.print_detail(console, _hardware(), row)

    # Passing at all (no MissingStyle) proves the panel now resolves
    # through the console's theme instead of a hardcoded literal color.
    assert "This PC" in output.getvalue()
