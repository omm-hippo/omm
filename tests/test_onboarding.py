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


def test_engine_choices_skip_already_installed_engines(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: key == "ollama")

    choices = onboarding._engine_choices()

    keys = [key for key, _ in choices]
    assert "ollama" not in keys
    assert "lmstudio" in keys


def test_engine_choices_tags_automation_level(monkeypatch):
    monkeypatch.setattr(linker, "is_engine_installed", lambda key: False)

    choices = dict(onboarding._engine_choices())

    assert "auto-install" in choices["ollama"]
    assert "not yet automated" in choices["lmstudio"]


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

    onboarding._install_selected_engines(console, ["lmstudio"])

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
