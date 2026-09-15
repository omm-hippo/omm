from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from rich.cells import cell_len

from omm import recommend_status, recommend_ui, theme as theme_mod
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


def test_generic_filename_uses_repository_model_name():
    assert recommend_ui.humanize_model_name({
        "repo_id": "ZhipuAI/glm-edge-4b-chat-gguf",
        "filename": "ggml-model-Q4_K_M.gguf",
    }) == "glm edge 4b chat"


def test_build_rows_adds_human_context_and_special_variant_warning():
    candidate = {
        "filename": "Gemma-3-1B-Heretic-Uncensored-Q4_K_M.gguf",
        "repo_id": "example/Gemma-3-1B-Heretic-Uncensored-GGUF",
        "description": "404,795 downloads on HuggingFace",
    }

    [row] = recommend_ui.build_rows([(candidate, 33.0)], ["example/model"])

    assert row.badge == "⚠ CAUTION"
    assert row.use_case == "—"
    assert row.model_type == "Unknown"
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


def test_repo_only_decoding_variant_gets_caution_instead_of_best_fit():
    candidate = {
        "filename": "Qwen3.6-27B-Q4_K_M.gguf",
        "repo_id": "unsloth/Qwen3.6-27B-MTP-GGUF",
    }
    [row] = recommend_ui.build_rows([(candidate, 6.0)], ["model"])
    assert row.display_name == "Qwen3.6 27B MTP"
    assert row.badge == "⚠ CAUTION"
    assert "runner requirements" in row.warning


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
    assert "Hugging Face" in rendered
    assert "Quantization  Q4_K_M" in rendered
    assert candidate["filename"] in rendered


def test_modelscope_detail_shows_selected_dynamic_quantization_and_source():
    candidate = {
        "filename": "Qwen3.8-27B-UD-Q4_K_M.gguf",
        "repo_id": "unsloth/Qwen3.8-27B-GGUF",
        "provider": "modelscope",
        "pipeline_tag": "text-generation",
        "tags": ["coding", "tool-use"],
    }
    [row] = recommend_ui.build_rows([(candidate, 6.0)], ["ms:unsloth/model"])
    output = StringIO()
    console = Console(file=output, width=120, theme=theme_mod.build_rich_theme("dark"))
    recommend_ui.print_detail(console, _hardware(), row)
    rendered = output.getvalue()
    assert "ModelScope" in rendered
    assert "Quantization  UD-Q4_K_M" in rendered
    assert candidate["filename"] in rendered
    assert "TYPE  LLM" in rendered
    assert "BEST FOR  Coding" in rendered
    assert "DECLARED FEATURES  Tool use" in rendered
    assert "Catalog metadata" in rendered


def test_narrow_choice_hides_memory_column_without_losing_status():
    candidate = {
        "filename": "TinyLlama-1.1B-Q4_K_M.gguf",
        "description": "Curated default",
        "pipeline_tag": "text-generation",
    }
    [row] = recommend_ui.build_rows([(candidate, 32.0)], ["tinyllama"])

    title = "".join(text for _, text in recommend_ui.choice_title(row, 70))
    header = recommend_ui.choice_header(70).plain

    assert "BEST FIT" in title
    assert "~32 tok/s" in title
    assert "General" in title
    assert "LLM" in title
    assert "TYPE" in header
    assert "MEMORY" not in header
    assert len(title) <= 70 - 4
    assert len(header) <= 70


def test_very_narrow_choice_hides_best_for_instead_of_clipping_the_line():
    candidate = {
        "filename": "TinyLlama-1.1B-Q4_K_M.gguf",
        "description": "Curated default",
    }
    [row] = recommend_ui.build_rows([(candidate, 32.0)], ["tinyllama"])

    title = "".join(text for _, text in recommend_ui.choice_title(row, 60))
    header = recommend_ui.choice_header(60).plain

    assert "BEST FOR" not in header
    assert "General" not in title
    assert len(title) <= 60 - 4
    assert len(header) <= 60


@pytest.mark.parametrize("width", [40, 47, 48, 60, 67, 68, 70, 80, 87, 88, 100, 120, 160])
@pytest.mark.parametrize(("model_type", "use_case"), [("VLM", "Translation"), ("Embedding", "—")])
def test_columns_fit_terminal_and_keep_header_alignment(width, model_type, use_case):
    candidate = {
        "filename": "Very-long-model-name-27B-Q4_K_M.gguf",
        "model_type": model_type, "use_case": use_case,
    }
    [row] = recommend_ui.build_rows([(candidate, 32.0)], ["test"])
    title = "".join(text for _, text in recommend_ui.choice_title(row, width))
    header = recommend_ui.choice_header(width).plain
    assert cell_len(title) <= width - 4
    assert cell_len(header) <= width
    if width >= 48:
        assert header.index("TYPE") - 3 == title.index(model_type)
        assert model_type in title
    else:
        assert "TYPE" not in header
    if width >= 68:
        assert header.index("BEST FOR") - 3 == title.index(row.use_case)
    else:
        assert "BEST FOR" not in header
    assert ("MEMORY" in header) == (width >= 88)


def test_selected_detail_retains_labels_hidden_on_small_terminals():
    [row] = recommend_ui.build_rows([
        ({"filename": "model-7b.gguf", "model_type": "VLM", "use_case": "documents", "capabilities": ["tools"]}, 30.0)
    ], ["test"])
    output = StringIO()
    console = Console(file=output, width=60, theme=theme_mod.build_rich_theme("dark"))
    recommend_ui.print_detail(console, _hardware(), row)
    text = output.getvalue()
    assert "TYPE" in text and "VLM" in text
    assert "BEST FOR" in text and "Documents" in text
    assert "Catalog metadata" in text
    assert "Tool use" in text


def test_wide_model_name_does_not_shift_type_column():
    [row] = recommend_ui.build_rows([
        ({"filename": "한국어-모델-이름-7B-Q4_K_M.gguf", "model_type": "LLM"}, 20.0)
    ], ["test"])
    title = "".join(text for _, text in recommend_ui.choice_title(row, 70))
    header = recommend_ui.choice_header(70).plain
    assert cell_len(title) <= 66
    assert cell_len(title.split("LLM")[0]) == header.index("TYPE") - 3


def test_recommend_screen_explains_caution_without_adding_a_table_column():
    output = StringIO()
    console = Console(
        file=output,
        width=70,
        color_system=None,
        force_terminal=False,
        theme=theme_mod.build_rich_theme("dark"),
    )

    recommend_ui.print_screen(console, _hardware(), 2, show_caution=True)

    rendered = output.getvalue()
    assert "CAUTION: Specialized or uncensored variant" in rendered
    assert "REASON" not in recommend_ui.choice_header(70).plain


def test_installed_candidate_has_installed_badge_and_real_source_detail():
    candidate = {
        "filename": "gpt-oss-20b-Q4_K_M.gguf",
        "repo_id": "unsloth/gpt-oss-20b-GGUF",
        "description": "test",
    }
    installation = recommend_status.InstallationStatus(
        True, True, ("ollama", "lmstudio"), candidate["filename"]
    )
    [row] = recommend_ui.build_rows(
        [(candidate, 30.0)],
        ["unsloth/gpt-oss-20b-GGUF:gpt-oss-20b-Q4_K_M.gguf"],
        [installation],
    )
    output = StringIO()
    console = Console(
        file=output,
        width=100,
        color_system=None,
        force_terminal=False,
        theme=theme_mod.build_rich_theme("dark"),
    )

    recommend_ui.print_detail(console, _hardware(), row)

    assert row.badge == "INSTALLED"
    assert "Already installed via OMM" in output.getvalue()


def test_model_identity_match_discloses_possible_package_difference():
    candidate = {
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "repo_id": "unsloth/Qwen3.5-9B-GGUF",
        "description": "test",
    }
    installation = recommend_status.InstallationStatus(
        True,
        True,
        ("ollama",),
        "qwen3.5-9b.gguf",
        "model_identity",
    )
    [row] = recommend_ui.build_rows(
        [(candidate, 14.0)],
        ["unsloth/Qwen3.5-9B-GGUF:Qwen3.5-9B-Q4_K_M.gguf"],
        [installation],
    )
    output = StringIO()
    console = Console(
        file=output,
        width=100,
        color_system=None,
        force_terminal=False,
        theme=theme_mod.build_rich_theme("dark"),
    )

    recommend_ui.print_detail(console, _hardware(), row)

    rendered = output.getvalue()
    assert "Same model and parameter size already installed via OMM" in rendered
    assert "different quantization or package" in rendered


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
