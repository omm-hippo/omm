import io

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
