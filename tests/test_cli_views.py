import io

from rich.console import Console

from omm.cli_views import print_engines
from omm.theme import build_rich_theme


def _engine(**overrides) -> dict:
    base = {
        "key": "ollama", "label": "Ollama", "installed": True,
        "package": {"manager": "brew", "version": "1.0"}, "package_error": None,
        "package_manageable": True,
        "api_status": "ready", "manual_url": "https://example.invalid",
    }
    base.update(overrides)
    return base


def _render(engines: list[dict]) -> str:
    console = Console(file=io.StringIO(), width=200, no_color=True, theme=build_rich_theme("no-color"))
    print_engines(console, engines, diagnostics=True)
    return console.file.getvalue()


def test_identical_package_errors_are_printed_once():
    error = "Could not read Homebrew's installed cask packages."
    engines = [
        _engine(key="ollama", label="Ollama", package=None, package_error=error),
        _engine(key="lmstudio", label="LM Studio", package=None, package_error=error),
    ]

    output = _render(engines)

    assert output.count(error) == 1


def test_distinct_package_errors_are_each_printed():
    engines = [
        _engine(key="ollama", label="Ollama", package=None, package_error="error one"),
        _engine(key="lmstudio", label="LM Studio", package=None, package_error="error two"),
    ]

    output = _render(engines)

    assert "error one" in output
    assert "error two" in output


def test_not_installed_engine_skips_redundant_manager_hint():
    engines = [
        _engine(key="jan", label="Jan", installed=False, package=None,
                api_status="diagnostics_unavailable"),
    ]

    output = _render(engines)

    assert "install with `omm engine install jan`." in output
    assert "package changes need an identified manager" not in output


def test_installed_engine_without_package_still_gets_manager_hint():
    engines = [
        _engine(key="ollama", label="Ollama", installed=True, package=None,
                api_status="ready"),
    ]

    output = _render(engines)

    assert "Ollama: package changes need an identified manager" in output


def test_installed_engine_with_no_package_manager_at_all_skips_the_hint():
    # koboldcpp/text-generation-webui have no brew/winget/flatpak identity at
    # all (#367): "package changes need an identified manager" is never a
    # real diagnosis for them, so it must not print even once installed.
    engines = [
        _engine(key="koboldcpp", label="KoboldCpp", installed=True, package=None,
                package_manageable=False, api_status="diagnostics_unavailable"),
    ]

    output = _render(engines)

    assert "package changes need an identified manager" not in output
