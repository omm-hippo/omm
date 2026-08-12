import io

import pytest
import typer
from rich.console import Console

from omm import linker, onboarding


def _console(width=100):
    return Console(file=io.StringIO(), width=width, force_terminal=True)


def test_print_banner_shows_ascii_art_when_wide_enough():
    console = _console(width=100)

    onboarding.print_banner(console)

    # The block-art banner has no literal "omm" substring (it's drawn from
    # U+2588 FULL BLOCK glyphs), so check for the art itself, not the word.
    assert "█" in console.file.getvalue()


def test_print_banner_falls_back_to_plain_text_when_narrow():
    console = _console(width=20)

    onboarding.print_banner(console)

    output = console.file.getvalue()
    assert "omm" in output.lower()
    # Falls back rather than wrapping/clipping the wide block-art lines.
    assert "█" not in output  # U+2588 FULL BLOCK, only in the big art


def test_print_hardware_summary_shows_os_and_ram(monkeypatch):
    from omm.hardware import HardwareInfo

    fake_info = HardwareInfo(
        os_name="TestOS",
        os_version="1.0",
        cpu="Test CPU",
        ram_total_gb=16.0,
        ram_available_gb=8.0,
        unified_memory=False,
        gpu_name="Test GPU",
        vram_total_gb=None,
        vram_free_gb=None,
    )
    monkeypatch.setattr(onboarding, "scan_hardware", lambda: fake_info)
    console = _console()

    onboarding.print_hardware_summary(console)

    output = console.file.getvalue()
    assert "TestOS" in output
    assert "16.0 GB" in output
    assert "Test GPU" in output


def test_engine_choices_includes_installed_engines_flagged(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: key == "ollama")

    choices = onboarding._engine_choices()

    by_key = {key: installed for key, _, installed in choices}
    assert by_key["ollama"] is True
    assert by_key["lmstudio"] is False
    assert len(choices) == len(linker.ENGINES)


def test_engine_choices_labels_carry_no_automation_tag(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)

    choices = onboarding._engine_choices()

    for _, label, _ in choices:
        assert "auto-install" not in label
        assert "not yet automated" not in label


def test_run_engine_checklist_all_installed_skips_prompt(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: True)
    console = _console()

    result = onboarding.run_engine_checklist(console)

    assert result == []
    assert "already installed" in console.file.getvalue()


def test_empty_selection_validator_warns_once_then_allows():
    validate = onboarding._build_empty_selection_validator()

    first = validate([])
    assert first is not True
    assert "space" in first.lower()

    second = validate([])
    assert second is True


def test_empty_selection_validator_allows_nonempty_immediately():
    validate = onboarding._build_empty_selection_validator()

    assert validate(["ollama"]) is True


def test_bracket_checkbox_indicators_swap_and_restore():
    import questionary.prompts.common as qcommon

    original_selected = qcommon.INDICATOR_SELECTED
    original_unselected = qcommon.INDICATOR_UNSELECTED

    with onboarding._bracket_checkbox_indicators():
        assert qcommon.INDICATOR_SELECTED == "[*]"
        assert qcommon.INDICATOR_UNSELECTED == "[ ]"

    assert qcommon.INDICATOR_SELECTED == original_selected
    assert qcommon.INDICATOR_UNSELECTED == original_unselected


def test_run_engine_checklist_cancels_reverse_video_on_checked_items(monkeypatch):
    """prompt_toolkit's own base style hardcodes ("selected", "reverse") for
    checked checkbox rows; questionary never overrides it by default. The
    wizard must pass its own style cancelling that attribute."""
    import questionary

    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_add_escape_to_cancel", lambda q: q)
    captured = {}

    class _FakeQuestion:
        def ask(self):
            return []

    def fake_checkbox(*args, **kwargs):
        captured["style"] = kwargs.get("style")
        return _FakeQuestion()

    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)
    console = _console()

    onboarding.run_engine_checklist(console)

    style = captured["style"]
    assert style is not None
    rules = dict(style.style_rules)
    assert "noreverse" in rules.get("selected", "")


def test_bracket_checkbox_indicators_restore_on_exception():
    import questionary.prompts.common as qcommon

    original_selected = qcommon.INDICATOR_SELECTED

    with pytest.raises(ValueError):
        with onboarding._bracket_checkbox_indicators():
            raise ValueError("boom")

    assert qcommon.INDICATOR_SELECTED == original_selected


def test_install_selected_engines_runs_installer_for_ollama(monkeypatch):
    console = _console()
    monkeypatch.setattr(
        linker,
        "install_engine",
        lambda key, on_output=None: linker.EngineInstallResult(key, "installed", "ok"),
    )

    onboarding._install_selected_engines(console, ["ollama"])

    output = console.file.getvalue()
    assert "Installing Ollama" in output
    assert "ok" in output


def test_install_selected_engines_links_out_for_unautomated_engine(monkeypatch):
    console = _console()

    onboarding._install_selected_engines(console, ["anythingllm"])

    output = console.file.getvalue()
    assert "isn't auto-installable yet" in output
    assert onboarding.COMPATIBLE_PROGRAMS_URL in output


def test_run_wizard_completes_with_no_engines_selected(monkeypatch):
    console = _console()
    monkeypatch.setattr(onboarding, "print_hardware_summary", lambda c: None)
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda c: [])

    onboarding.run_wizard(console)

    assert "Setup complete" in console.file.getvalue()


def test_run_wizard_aborts_when_engine_checklist_is_cancelled(monkeypatch):
    """A Ctrl+C/Escape abort during the checklist must surface as
    `None`, not `[]` - `run_wizard` must not print "Setup complete" or let
    the caller mark onboarding_completed=True on cancel."""
    console = _console()
    monkeypatch.setattr(onboarding, "print_hardware_summary", lambda c: None)
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda c: None)

    with pytest.raises(typer.Abort):
        onboarding.run_wizard(console)

    assert "Setup complete" not in console.file.getvalue()


def test_run_engine_checklist_requires_tty(monkeypatch):
    """Non-interactive stdin must exit cleanly with a clear message rather
    than falling through to a raw questionary crash/traceback."""
    console = _console()
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: False)

    with pytest.raises(typer.Exit):
        onboarding.run_engine_checklist(console)

    assert "interactive terminal" in console.file.getvalue()


def test_install_selected_engines_prints_raw_installer_output_without_markup_errors(monkeypatch):
    """Installer output must pass through Rich's console.print raw (no
    markup interpretation): a `[sudo] password:` line must not have its
    bracketed token eaten, and a line shaped like `[/dim]` must not raise
    rich.errors.MarkupError and crash the wizard."""
    console = _console()
    captured = {}

    def fake_install_engine(key, on_output=None):
        captured["on_output"] = on_output
        return linker.EngineInstallResult(key, "installed", "ok")

    monkeypatch.setattr(linker, "install_engine", fake_install_engine)

    onboarding._install_selected_engines(console, ["ollama"])

    on_output = captured["on_output"]
    on_output("[sudo] password:")
    on_output("weird [/dim] text")

    output = console.file.getvalue()
    assert "[sudo] password:" in output
    assert "weird [/dim] text" in output
