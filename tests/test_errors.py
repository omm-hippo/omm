import io

from rich.console import Console

from omm import errors, theme


def _console() -> Console:
    return Console(file=io.StringIO(), theme=theme.build_rich_theme("dark"), width=200)


def test_print_cli_error_prints_cause_line():
    console = _console()
    errors.print_cli_error(console, "Ollama is not installed.")
    assert "Ollama is not installed." in console.file.getvalue()


def test_print_cli_error_prints_fix_line_with_arrow_prefix_when_given():
    console = _console()
    errors.print_cli_error(console, "Ollama is not installed.", fix="Run `omm setup` to install it.")
    output = console.file.getvalue()
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].strip() == "Ollama is not installed."
    assert lines[1].strip() == "→ Run `omm setup` to install it."


def test_print_cli_error_omits_fix_line_when_none():
    console = _console()
    errors.print_cli_error(console, "Ollama is not installed.")
    output = console.file.getvalue()
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
