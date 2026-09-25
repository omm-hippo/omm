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


def test_no_package_manager_falls_back_to_the_apis_own_reported_version():
    # #368: an engine with no brew/winget/flatpak identity (or one installed
    # outside its package manager) can still tell us its version via its own
    # API - display only, never fed into update/uninstall command assembly.
    engines = [
        _engine(key="ollama", label="Ollama", package=None,
                package_manageable=False, runtime_version="0.5.1"),
    ]

    output = _render(engines)

    assert "reported by API / 0.5.1" in output
    assert "Not identified" not in output


def test_detected_version_is_used_even_when_the_api_is_off():
    # #368: an Info.plist read or `ollama --version` works without the
    # daemon running, unlike the API-based fallback above.
    engines = [
        _engine(key="ollama", label="Ollama", package=None, package_manageable=False,
                api_status="server_unavailable", detected_version="0.33.1"),
    ]

    output = _render(engines)

    assert "detected / 0.33.1" in output


def test_detected_version_is_preferred_over_the_apis_reported_version():
    engines = [
        _engine(key="ollama", label="Ollama", package=None, package_manageable=False,
                detected_version="0.33.1", runtime_version="0.5.1"),
    ]

    output = _render(engines)

    assert "detected / 0.33.1" in output
    assert "reported by API" not in output


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


def test_package_error_is_followed_by_its_fix_line():
    # #388: a lookup failure must come with a concrete next step.
    engines = [
        _engine(package=None, package_error="WinGet package lookup failed (5).",
                package_fix={"kind": "query_failed",
                             "fix": "run `winget list --id Ollama.Ollama --exact --source winget` to see WinGet's full message."}),
    ]

    output = _render(engines)

    lines = output.splitlines()
    error_at = next(i for i, line in enumerate(lines) if "WinGet package lookup failed (5)." in line)
    assert lines[error_at + 1].startswith("→ Ollama: run `winget list --id Ollama.Ollama")
    # The generic manager hint is redundant once the specific fix is shown.
    assert "package changes need an identified manager" not in output


def test_missing_manager_fix_replaces_generic_manager_hint():
    engines = [
        _engine(package=None, package_fix={"kind": "manager_missing",
                                           "fix": "Homebrew was not found. It is optional."}),
    ]

    output = _render(engines)

    assert "Ollama: Homebrew was not found. It is optional." in output
    assert "package changes need an identified manager" not in output


def test_uninstalled_engine_fix_only_when_its_error_line_is_shown():
    error = "WinGet package lookup did not finish."
    engines = [
        _engine(key="ollama", label="Ollama", package=None, package_error=error,
                package_fix={"kind": "timeout", "fix": "ollama fix."}),
        _engine(key="jan", label="Jan", installed=False, package=None, package_error=error,
                package_fix={"kind": "timeout", "fix": "jan fix."}),
    ]
    assert "jan fix." not in _render(engines)
    # Nothing installed at all: the one error line still gets a next step.
    assert "→ Jan: jan fix." in _render(engines[1:])
