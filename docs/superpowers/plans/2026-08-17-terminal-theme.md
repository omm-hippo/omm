# Terminal Theme Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `omm setup` detect/recommend a terminal color theme (light/dark/high-contrast/no-color), let the user confirm or override it, apply it everywhere the CLI prints colored output, and let it be changed later via `omm setting theme`.

**Architecture:** A new `omm/theme.py` module defines six semantic style roles (`error`, `warning`, `success`, `accent`, `muted`, `value`) as a `rich.theme.Theme` per preset. The CLI's global `console`/`err_console` re-apply the saved preset on every invocation (mirroring how `--no-color` already works). All ~200 existing hardcoded-color call sites in `cli.py`/`onboarding.py` are renamed to the semantic role names via a one-time scripted string replacement; `recommend_ui.py`'s own `ACCENT`/`SUCCESS`/`WARNING`/`MUTED` constants (plus 3 stray literal sites in the same "hardware panel" region) are pointed at the same roles, while its separately-designed arrow-list palette (`_ROW_*`, `SELECT_STYLE`) is left untouched.

**Tech Stack:** Python, rich (`Theme`, `Style`, `Console.push_theme`), typer, questionary, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-terminal-theme-design.md`

## Global Constraints

- Four presets only: `light`, `dark`, `high-contrast`, `no-color`. No custom/user-defined colors.
- `light` preset must reproduce today's exact hardcoded values (error=bold red, warning=yellow, success=bold green, accent=bold blue, muted=dim, value=white) — it is the existing behavior renamed, not redesigned.
- Detection is env-var heuristic only (`COLORFGBG`), never an OSC terminal query. Always shows all four options and lets the user pick, regardless of the guess.
- `no-color` reuses the existing `console.no_color` flag mechanism (`cli.py`'s `--no-color` flag already sets this) — it is not a fifth `Theme` object.
- `recommend_ui.py`'s `_ROW_*` constants, `SELECT_STYLE`, and its own `COLORS_ENABLED`/`NO_COLOR` check are out of scope — do not touch them.
- The ASCII banner in `onboarding.print_banner` (`bold blue`) stays hardcoded — it prints before a theme is chosen.

---

### Task 1: `theme.py` — roles, presets, detection

**Files:**
- Create: `src/omm/theme.py`
- Test: `tests/test_theme.py`

**Interfaces:**
- Produces: `ROLES: tuple[str, ...]`, `THEME_NAMES: tuple[str, ...]` (`("light", "dark", "high-contrast", "no-color")`), `build_rich_theme(name: str) -> Theme | None`, `detect_recommended() -> str`, `apply_theme_to_console(console: Console, name: str) -> None`, `print_theme_preview(console: Console, name: str) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_theme.py
import io

import pytest
from rich.console import Console

from omm import theme


def test_theme_names_are_the_four_fixed_presets():
    assert theme.THEME_NAMES == ("light", "dark", "high-contrast", "no-color")


@pytest.mark.parametrize("name", ["light", "dark", "high-contrast"])
def test_build_rich_theme_defines_every_role_and_its_bold_variant(name):
    rich_theme = theme.build_rich_theme(name)
    for role in theme.ROLES:
        assert role in rich_theme.styles
        assert f"bold {role}" in rich_theme.styles
        assert rich_theme.styles[f"bold {role}"].bold is True


def test_build_rich_theme_returns_none_for_no_color():
    assert theme.build_rich_theme("no-color") is None


def test_light_preset_matches_todays_hardcoded_colors():
    styles = theme.build_rich_theme("light").styles
    assert styles["error"].color.name == "red"
    assert styles["error"].bold is True
    assert styles["warning"].color.name == "yellow"
    assert styles["warning"].bold is not True
    assert styles["success"].color.name == "green"
    assert styles["success"].bold is True
    assert styles["accent"].color.name == "blue"
    assert styles["accent"].bold is True
    assert styles["muted"].dim is True
    assert styles["value"].color.name == "white"


def test_dark_preset_only_differs_from_light_in_accent():
    light = theme.build_rich_theme("light").styles
    dark = theme.build_rich_theme("dark").styles
    for role in theme.ROLES:
        if role == "accent":
            assert dark[role].color.name != light[role].color.name
        else:
            assert dark[role].color == light[role].color
            assert dark[role].bold == light[role].bold


def test_detect_recommended_reads_colorfgbg_light_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect_recommended() == "light"


def test_detect_recommended_reads_colorfgbg_dark_background(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert theme.detect_recommended() == "dark"


def test_detect_recommended_defaults_to_dark_when_unset(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_recommended() == "dark"


def test_detect_recommended_defaults_to_dark_on_garbage_value(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "not-a-number")
    assert theme.detect_recommended() == "dark"


def test_apply_theme_to_console_sets_no_color_for_no_color_preset():
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "no-color")
    assert console.no_color is True


def test_apply_theme_to_console_pushes_theme_for_named_preset():
    console = Console(file=io.StringIO())
    theme.apply_theme_to_console(console, "dark")
    assert console.no_color is False
    console.print("[accent]hello[/accent]")
    assert "hello" in console.file.getvalue()


def test_print_theme_preview_renders_all_roles_without_raising():
    console = Console(file=io.StringIO(), force_terminal=True)
    for name in theme.THEME_NAMES:
        theme.print_theme_preview(console, name)
    output = console.file.getvalue()
    for role in theme.ROLES:
        assert role in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_theme.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omm.theme'`

- [ ] **Step 3: Write `src/omm/theme.py`**

```python
"""Semantic color roles for CLI output, resolved against one of four
fixed presets so the same markup (`[error]`, `[accent]`, ...) renders
correctly regardless of the user's terminal background.

`light` reproduces the exact colors this codebase used before theming
existed (tuned against one developer's own light-beige terminal); only
`accent` changes for `dark`, since plain terminal blue is classically
low-contrast on a black background. `no-color` is not a `Theme` at all
- it reuses the `Console.no_color` flag the `--no-color` CLI option
already sets."""

from __future__ import annotations

import os

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

ROLES = ("error", "warning", "success", "accent", "muted", "value")
THEME_NAMES = ("light", "dark", "high-contrast", "no-color")

_BASE_STYLES: dict[str, dict[str, Style]] = {
    "light": {
        "error": Style(color="red", bold=True),
        "warning": Style(color="yellow"),
        "success": Style(color="green", bold=True),
        "accent": Style(color="blue", bold=True),
        "muted": Style(dim=True),
        "value": Style(color="white"),
    },
    "dark": {
        "error": Style(color="red", bold=True),
        "warning": Style(color="yellow"),
        "success": Style(color="green", bold=True),
        # Plain terminal blue reads as a near-illegible navy on a black
        # background (the classic Ubuntu-bash directory-color problem);
        # every other role carries over unchanged from `light`.
        "accent": Style(color="bright_cyan", bold=True),
        "muted": Style(dim=True),
        "value": Style(color="white"),
    },
    "high-contrast": {
        "error": Style(color="white", bgcolor="red", bold=True),
        "warning": Style(color="black", bgcolor="yellow", bold=True),
        "success": Style(color="black", bgcolor="green", bold=True),
        "accent": Style(color="cyan", bold=True),
        "muted": Style(color="white"),
        "value": Style(color="white", bold=True),
    },
}


def build_rich_theme(name: str) -> Theme | None:
    """`None` for "no-color" (and any unrecognized name) - the caller is
    expected to set `console.no_color = True` instead in that case."""
    base = _BASE_STYLES.get(name)
    if base is None:
        return None
    styles: dict[str, Style] = {}
    for role, style in base.items():
        styles[role] = style
        # `recommend_ui.py` and a couple of `cli.py`/`onboarding.py` sites
        # build style strings like f"bold {ACCENT}" - registering the
        # compound key lets those keep working without every call site
        # needing to know whether its role is already bold.
        styles[f"bold {role}"] = style + Style(bold=True)
    return Theme(styles)


def detect_recommended() -> str:
    """Best-effort guess only - always "light" or "dark", never raises.
    No OSC terminal queries (unreliable inside multiplexers, can hang);
    this only pre-selects the picker cursor, the user always confirms."""
    raw = os.environ.get("COLORFGBG", "")
    if raw:
        try:
            bg = int(raw.split(";")[-1])
        except ValueError:
            bg = None
        if bg is not None:
            return "light" if bg == 7 or 9 <= bg <= 15 else "dark"
    return "dark"


def apply_theme_to_console(console: Console, name: str) -> None:
    console.no_color = name == "no-color"
    rich_theme = build_rich_theme(name)
    if rich_theme is not None:
        console.push_theme(rich_theme)


def print_theme_preview(console: Console, name: str) -> None:
    """One line per role, rendered in `name`'s actual styles, sharing
    `console`'s width/file/terminal-ness so the preview shows real color
    on the user's real screen."""
    preview = Console(
        file=console.file,
        width=console.size.width,
        force_terminal=console.is_terminal,
        no_color=(name == "no-color"),
        theme=build_rich_theme(name),
        highlight=False,
    )
    preview.print(f"[bold]{name}[/bold]")
    for role in ROLES:
        preview.print(f"  [{role}]{role}[/{role}]  sample text")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_theme.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/theme.py tests/test_theme.py
git commit -m "feat: add omm.theme module with 4 presets and detection heuristic"
```

---

### Task 2: `config.py` default theme key

**Files:**
- Modify: `src/omm/config.py`
- Test: `tests/test_config_onboarding.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DEFAULT_CONFIG["theme"] == "dark"`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config_onboarding.py
def test_default_config_includes_dark_theme_fallback():
    from omm import config

    assert config.DEFAULT_CONFIG["theme"] == "dark"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_onboarding.py -k default_config_includes_dark_theme -v`
Expected: FAIL with `KeyError: 'theme'`

- [ ] **Step 3: Add the default**

In `src/omm/config.py`, inside `DEFAULT_CONFIG` (after `"onboarding_completed": True,`):

```python
    "theme": "dark",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_onboarding.py -k default_config_includes_dark_theme -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/omm/config.py tests/test_config_onboarding.py
git commit -m "feat: add theme key to default config"
```

---

### Task 3: Apply the saved theme to `cli.py`'s console on every invocation

**Files:**
- Modify: `src/omm/cli.py` (import list around line 36-58, and `_root` around line 376-391)
- Modify: `tests/test_cli_global_flags.py` (bare `Console(...)` replacements need a theme too, now that command output uses semantic role markup)

**Interfaces:**
- Consumes: `theme.apply_theme_to_console` (Task 1), `config.load_config` (already imported as `load_config`).

- [ ] **Step 1: Add the import**

In `src/omm/cli.py`, inside the `from omm import (...)` block (line 36-58), insert alphabetically:

```python
    telemetry,
    theme as theme_mod,
    trust,
```

- [ ] **Step 2: Write the failing test**

```python
# add to tests/test_cli_global_flags.py
def test_root_callback_applies_saved_theme_to_console(isolated_omm_home):
    from omm import config

    config.update_config(theme="high-contrast")

    result = runner.invoke(cli.app, ["setting", "version"])

    assert result.exit_code == 0, result.stdout
    assert cli.console.no_color is False
```

(This exercises the plumbing; Task 6's wizard test and Task 7's `setting theme` test cover the actual color values end-to-end.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_cli_global_flags.py -k applies_saved_theme -v`
Expected: FAIL (theme never applied, or `AttributeError` if `theme_mod` doesn't exist yet in the test's import path — either way, not passing)

- [ ] **Step 4: Wire it into `_root`**

In `src/omm/cli.py`, at the top of `_root` (replace the existing body's start, keeping everything else):

```python
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    json_flag: Annotated[bool, typer.Option("--json", help="Print output as JSON where supported.")] = False,
    yes_flag: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts. For scripting.")] = False,
    quiet_flag: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress progress bars and background status/hint lines (errors and results still print).")] = False,
    no_color_flag: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
) -> None:
    opts = ctx.ensure_object(GlobalOptions)
    opts.json = opts.json or json_flag
    opts.yes = opts.yes or yes_flag
    opts.quiet = opts.quiet or quiet_flag
    opts.no_color = opts.no_color or no_color_flag
    theme_mod.apply_theme_to_console(console, load_config().get("theme", "dark"))
    theme_mod.apply_theme_to_console(err_console, load_config().get("theme", "dark"))
    if opts.no_color:
        console.no_color = True
        err_console.no_color = True
```

(`--no-color` still wins over a saved non-`no-color` preset since it's applied after.) `_root` is Typer's top-level app callback (`invoke_without_command=True`), so it runs exactly once before every subcommand dispatch regardless of how the app is invoked - unlike `--no-color` itself (which the `global_flags` decorator at cli.py:220-224 also re-applies, because Click parses a flag placed after the subcommand name, e.g. `omm setting telemetry --no-color`, separately from one placed before it), theme is read from config rather than a per-command flag, so one application point in `_root` is sufficient - don't duplicate it into `global_flags`.

- [ ] **Step 5: Fix the bare-Console test fixtures**

In `tests/test_cli_global_flags.py`, wherever `cli.console`/`cli.err_console` are monkeypatched with a bare `Console(...)` (around line 40-42, inside `test_no_color_flag_disables_ansi_codes`), give them a theme so any `[error]`/`[accent]`/etc. markup those tests exercise doesn't raise `MissingStyle`. Use the **`light`** preset specifically, not `dark`: that test asserts the literal ANSI code `\x1b[34m` (blue) for the "Field" column, which after Task 4's rename resolves through the `accent` role - `light`'s accent is blue (`\x1b[34m`, matching the existing assertion unchanged), while `dark`'s accent is `bright_cyan` (a different code), which would make this pre-existing assertion fail for a reason that has nothing to do with what the test is actually checking.

```python
from omm import theme as theme_mod
...
    monkeypatch.setattr(
        cli, "console",
        Console(force_terminal=True, highlight=False, theme=theme_mod.build_rich_theme("light")),
    )
    monkeypatch.setattr(
        cli, "err_console",
        Console(stderr=True, force_terminal=True, highlight=False, theme=theme_mod.build_rich_theme("light")),
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_cli_global_flags.py -v`
Expected: PASS (all tests in the file, not just the new one — this confirms the bare-console fix didn't break the existing no-color/json/quiet tests)

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_global_flags.py
git commit -m "feat: apply saved theme to CLI console on every invocation"
```

---

### Task 4: Migrate `cli.py` and `onboarding.py` to semantic role markup

**Files:**
- Modify: `src/omm/cli.py` (~195 sites)
- Modify: `src/omm/onboarding.py` (~8 sites)
- Modify: `tests/test_onboarding.py` (the shared `_console()` helper)

This is a one-time scripted rename, not a manual edit — every occurrence already carries a consistent meaning (see spec section 6), so a literal string substitution is safe. The two-word `[bold X]` markup and `f"bold {X}"`-style compound keys are registered directly by Task 1's `build_rich_theme`, but `cli.py`/`onboarding.py` never build styles that way (only `recommend_ui.py`, handled in Task 5) - here every `[bold X]` markup tag collapses to the plain single-word role tag, since the role's `Theme` entry already encodes bold where needed.

- [ ] **Step 1: Run the script**

Run this once from the repo root (not committed as a file - `python3 -c` is enough):

```bash
python3 -c "
import pathlib

MARKUP = [
    ('[bold red]', '[error]'), ('[/bold red]', '[/error]'),
    ('[red]', '[error]'), ('[/red]', '[/error]'),
    ('[yellow]', '[warning]'), ('[/yellow]', '[/warning]'),
    ('[bold green]', '[success]'), ('[/bold green]', '[/success]'),
    ('[green]', '[success]'), ('[/green]', '[/success]'),
    ('[bold blue]', '[accent]'), ('[/bold blue]', '[/accent]'),
    ('[blue]', '[accent]'), ('[/blue]', '[/accent]'),
    ('[dim]', '[muted]'), ('[/dim]', '[/muted]'),
]
STYLE_KWARGS = [
    ('style=\"blue\"', 'style=\"accent\"'),
    ('style=\"white\"', 'style=\"value\"'),
    ('style=\"cyan\"', 'style=\"accent\"'),
    ('style=\"dim\"', 'style=\"muted\"'),
]

for relpath in ('src/omm/cli.py', 'src/omm/onboarding.py'):
    path = pathlib.Path(relpath)
    text = path.read_text()
    for old, new in MARKUP + STYLE_KWARGS:
        text = text.replace(old, new)
    path.write_text(text)
"
```

- [ ] **Step 2: Verify no literal color tokens remain**

Run:

```bash
grep -nE '\[(bold )?(red|green|yellow|blue|cyan)\]|\[/(bold )?(red|green|yellow|blue|cyan)\]|style="(red|green|yellow|blue|cyan|white|dim)"' src/omm/cli.py src/omm/onboarding.py
```

Expected: no output. If anything prints, hand-fix that line (the script's replacement list is exhaustive against a `grep`-verified audit at design time, but re-verify against the actual current file - if a genuinely new pattern turns up, add it to the list and re-run rather than hand-patching only that one line, so the mapping stays documented).

- [ ] **Step 3: Fix the shared test console helper**

In `tests/test_onboarding.py`, the `_console()` helper (used by ~15 tests in the file) now needs a theme registered, or every renamed `[error]`/`[accent]`/`[success]`/`[muted]` markup print in `onboarding.py` will raise `rich.errors.MissingStyle` under test:

```python
import io

import pytest
import typer
from rich.console import Console

from omm import linker, onboarding, theme as theme_mod


def _console(width=100):
    return Console(
        file=io.StringIO(), width=width, force_terminal=True,
        theme=theme_mod.build_rich_theme("dark"),
    )
```

- [ ] **Step 4: Run the full test suite for both files**

Run: `pytest tests/test_onboarding.py tests/test_cli_onboarding.py tests/test_cli_setting.py tests/test_cli_telemetry_config.py tests/test_cli_help_version.py -v`
Expected: PASS. (These are the suites most likely to exercise renamed markup; a broader full-suite run happens in Task 8.)

- [ ] **Step 5: Review the diff for mis-mapped sites**

Run: `git diff --stat src/omm/cli.py src/omm/onboarding.py` then skim `git diff src/omm/cli.py` for any role that reads oddly in context (e.g. a `[warning]` on what's actually a hard failure, which should be `[error]`) - the design's audit found the existing colors already consistent, but confirm against the actual diff rather than assuming.

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py src/omm/onboarding.py tests/test_onboarding.py
git commit -m "refactor: migrate cli.py/onboarding.py to semantic theme role markup"
```

---

### Task 5: Point `recommend_ui.py`'s color constants at the new roles

**Files:**
- Modify: `src/omm/recommend_ui.py:19-22, 181, 307, 323`
- Test: `tests/test_recommend_ui.py`

Only the four module constants and three literal sites in the same "hardware panel"/detail-view region change. `_ROW_*`, `SELECT_STYLE`, and `COLORS_ENABLED` (the arrow-navigable choice list's own separately-designed palette) are untouched.

**Interfaces:**
- Consumes: role names `"accent"`, `"success"`, `"warning"`, `"muted"`, `"value"` (Task 1).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_recommend_ui.py
from omm import theme as theme_mod


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
```

(Add `from rich.console import Console` / `from io import StringIO` if not already imported at the top of the file - both already are, per the existing `test_recommend_screen_renders_hardware_table_and_selected_detail` test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recommend_ui.py -k hardware_panel_uses_theme_roles -v`
Expected: FAIL with `rich.errors.MissingStyle: no style called "bold blue"` (or similar) - today's `ACCENT = "blue"` isn't a theme-registered name.

- [ ] **Step 3: Update `recommend_ui.py`**

Change lines 19-22:

```python
ACCENT = "accent"
SUCCESS = "success"
WARNING = "warning"
MUTED = "muted"
```

Change line 181 (`_hardware_value`):

```python
    text.append(value, style="bold value")
```

Change line 307 (inside `print_detail`'s `Group(...)`):

```python
                Text(row.description, style="value"),
```

Change line 323:

```python
    console.print(
        "[muted]Predicted speed is an estimate; actual performance can vary by runtime settings.[/muted]"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_recommend_ui.py -v`
Expected: PASS (full file, since existing tests at lines ~50-70 build a plain `Console(color_system=None)` with no theme and will now also need a theme - if `test_recommend_screen_renders_hardware_table_and_selected_detail` fails with `MissingStyle`, add `theme=theme_mod.build_rich_theme("dark")` to that test's `Console(...)` call too, same fix as Step 3 pattern.)

- [ ] **Step 5: Commit**

```bash
git add src/omm/recommend_ui.py tests/test_recommend_ui.py
git commit -m "refactor: point recommend_ui's hardware-panel colors at theme roles"
```

---

### Task 6: Wizard theme step in `omm setup`

**Files:**
- Modify: `src/omm/onboarding.py`
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `theme.THEME_NAMES`, `theme.detect_recommended`, `theme.print_theme_preview`, `theme.apply_theme_to_console` (Task 1); `config.update_config` (existing).
- Produces: `run_theme_step(console: Console) -> str` (the chosen/kept theme name), wired into `run_wizard` between `print_banner` and `print_hardware_summary`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_onboarding.py
def test_run_theme_step_skips_prompt_and_keeps_guess_when_not_a_tty(monkeypatch, isolated_omm_home):
    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(onboarding.theme, "detect_recommended", lambda: "dark")
    console = _console()

    result = onboarding.run_theme_step(console)

    assert result == "dark"
    from omm import config
    assert config.load_config()["theme"] == "dark"


def test_run_theme_step_saves_the_users_pick(monkeypatch, isolated_omm_home):
    import questionary

    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_add_escape_to_cancel", lambda q: q)
    monkeypatch.setattr(onboarding.theme, "detect_recommended", lambda: "dark")

    class _FakeQuestion:
        def ask(self):
            return "high-contrast"

    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakeQuestion())
    console = _console()

    result = onboarding.run_theme_step(console)

    assert result == "high-contrast"
    from omm import config
    assert config.load_config()["theme"] == "high-contrast"


def test_run_theme_step_falls_back_to_recommendation_on_cancel(monkeypatch, isolated_omm_home):
    import questionary

    monkeypatch.setattr(onboarding, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(onboarding, "_add_escape_to_cancel", lambda q: q)
    monkeypatch.setattr(onboarding.theme, "detect_recommended", lambda: "light")

    class _FakeQuestion:
        def ask(self):
            return None

    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakeQuestion())
    console = _console()

    result = onboarding.run_theme_step(console)

    assert result == "light"


def test_run_wizard_runs_theme_step_before_hardware_summary(monkeypatch):
    order = []
    monkeypatch.setattr(onboarding, "run_theme_step", lambda c: order.append("theme") or "dark")
    monkeypatch.setattr(onboarding, "print_hardware_summary", lambda c: order.append("hardware"))
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda c: order.append("engines") or [])
    console = _console()

    onboarding.run_wizard(console)

    assert order == ["theme", "hardware", "engines"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_onboarding.py -k run_theme_step -v`
Expected: FAIL with `AttributeError: module 'omm.onboarding' has no attribute 'run_theme_step'`

- [ ] **Step 3: Implement it**

In `src/omm/onboarding.py`, add the import (alongside the existing `from omm import linker` line):

```python
from omm import config as config_mod
from omm import linker
from omm import theme
```

Add the function (near `run_engine_checklist`):

```python
def run_theme_step(console: Console) -> str:
    """Returns the theme name in effect after this step - either the
    user's explicit pick, or the detected guess if stdin isn't a TTY or
    the prompt was cancelled. Unlike `run_engine_checklist`, a cancel
    here is not treated as aborting the whole wizard - nothing
    destructive has happened yet, so falling back to the guess is safe."""
    recommended = theme.detect_recommended()

    if not _stdin_is_tty():
        config_mod.update_config(theme=recommended)
        theme.apply_theme_to_console(console, recommended)
        return recommended

    import questionary

    for name in theme.THEME_NAMES:
        label = f"{name} (recommended)" if name == recommended else name
        console.print(f"\n[bold]{label}[/bold]")
        theme.print_theme_preview(console, name)

    question = _add_escape_to_cancel(
        questionary.select(
            "Pick a color theme for omm's output:",
            choices=list(theme.THEME_NAMES),
            default=recommended,
        )
    )
    chosen = question.ask() or recommended
    config_mod.update_config(theme=chosen)
    theme.apply_theme_to_console(console, chosen)
    return chosen
```

Update `run_wizard` to call it first:

```python
def run_wizard(console: Console) -> None:
    print_banner(console)
    run_theme_step(console)
    print_hardware_summary(console)
    selected = run_engine_checklist(console)
    ...
```

(keep the rest of `run_wizard` exactly as-is below this point).

- [ ] **Step 4: Fix the two pre-existing `run_wizard` tests**

`test_run_wizard_completes_with_no_engines_selected` and
`test_run_wizard_aborts_when_engine_checklist_is_cancelled` (already in
`tests/test_onboarding.py`, written before this task) call
`onboarding.run_wizard(console)` without mocking `run_theme_step`. Now
that `run_wizard` calls it for real, both would silently write to the
developer's actual `~/.omm/config.json` (no `isolated_omm_home` fixture
in either test today). Add a `run_theme_step` mock to both, matching
how they already mock `print_hardware_summary`/`run_engine_checklist`:

```python
def test_run_wizard_completes_with_no_engines_selected(monkeypatch):
    console = _console()
    monkeypatch.setattr(onboarding, "run_theme_step", lambda c: "dark")
    monkeypatch.setattr(onboarding, "print_hardware_summary", lambda c: None)
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda c: [])

    onboarding.run_wizard(console)

    assert "Setup complete" in console.file.getvalue()


def test_run_wizard_aborts_when_engine_checklist_is_cancelled(monkeypatch):
    console = _console()
    monkeypatch.setattr(onboarding, "run_theme_step", lambda c: "dark")
    monkeypatch.setattr(onboarding, "print_hardware_summary", lambda c: None)
    monkeypatch.setattr(onboarding, "run_engine_checklist", lambda c: None)

    with pytest.raises(typer.Abort):
        onboarding.run_wizard(console)

    assert "Setup complete" not in console.file.getvalue()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_onboarding.py -v`
Expected: PASS (full file)

- [ ] **Step 6: Commit**

```bash
git add src/omm/onboarding.py tests/test_onboarding.py
git commit -m "feat: add theme selection step to the omm setup wizard"
```

---

### Task 7: `omm setting theme` subcommand + bare-menu entry

**Files:**
- Modify: `src/omm/cli.py` (new `@setting_app.command(name="theme")`, plus `setting_menu`)
- Test: `tests/test_cli_setting.py`

**Interfaces:**
- Consumes: `theme.THEME_NAMES`, `theme.print_theme_preview` (Task 1); `onboarding.run_theme_step` is NOT reused here (that one always prompts and always saves; this command supports a non-interactive `--set` path setup doesn't need).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_cli_setting.py
def test_setting_theme_set_saves_and_shows_table(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "theme", "--set", "high-contrast"])

    assert result.exit_code == 0, result.stdout
    assert "high-contrast" in result.stdout
    assert config.load_config()["theme"] == "high-contrast"


def test_setting_theme_rejects_unknown_name(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "theme", "--set", "purple"])

    assert result.exit_code == 1
    # err_console output lands on result.stderr, not result.stdout - see
    # tests/test_cli_help_version.py's "No such command" test for the
    # same CliRunner convention in this suite.
    assert "light, dark, high-contrast, no-color" in result.stderr


def test_setting_theme_bare_shows_current_value(isolated_omm_home):
    config.update_config(theme="dark")

    result = runner.invoke(cli.app, ["setting", "theme"])

    assert result.exit_code == 0, result.stdout
    assert "dark" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_setting.py -k setting_theme -v`
Expected: FAIL with a typer "No such command 'theme'" error

- [ ] **Step 3: Add the command**

In `src/omm/cli.py`, after `configure_version` (before `calibrate`):

```python
@setting_app.command(name="theme")
@global_flags
def configure_theme(
    set_name: str = typer.Option(
        None,
        "--set",
        help="One of: " + ", ".join(theme_mod.THEME_NAMES),
    ),
) -> None:
    """Show or change the color theme applied to omm's output."""
    current = load_config()
    if set_name is not None:
        if set_name not in theme_mod.THEME_NAMES:
            err_console.print(
                f"[error]--set must be one of: {', '.join(theme_mod.THEME_NAMES)}.[/error]"
            )
            raise typer.Exit(1)
        current = config_mod.update_config(theme=set_name)
        theme_mod.apply_theme_to_console(console, set_name)
        theme_mod.apply_theme_to_console(err_console, set_name)
    table = Table(title="Color theme", show_header=False)
    table.add_column("Field", style="accent")
    table.add_column("Value")
    table.add_row("Theme", str(current.get("theme", "dark")))
    console.print(table)
```

- [ ] **Step 4: Add it to the bare TUI menu**

In `setting_menu` (`src/omm/cli.py`), add a choice and a branch. In the `choices=[...]` list, after the `version` choice:

```python
                    questionary.Choice(
                        f"Theme (current: {current.get('theme', 'dark')})", value="theme"
                    ),
```

And in the `if choice == ...` chain, after the `version` branch:

```python
        elif choice == "theme":
            action = _ask_select(
                questionary.select(
                    f"Theme (current: {current.get('theme', 'dark')}):",
                    choices=[*theme_mod.THEME_NAMES, "← Back"],
                )
            )
            if action is not None and action != "← Back":
                configure_theme(set_name=action)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cli_setting.py -v`
Expected: PASS (full file)

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_setting.py
git commit -m "feat: add omm setting theme subcommand and TUI menu entry"
```

---

### Task 8: Regression guard against reintroducing hardcoded colors

**Files:**
- Create: `tests/test_theme_no_hardcoded_colors.py`

**Interfaces:**
- Consumes: nothing (pure grep-based static check over the two source files).

- [ ] **Step 1: Write the test**

```python
"""Regression guard: cli.py and onboarding.py must route all color
through omm.theme's semantic roles, never a literal color name, so a
future PR can't silently reintroduce a hardcoded-terminal-color bug."""

import re
from pathlib import Path

_LITERAL_MARKUP = re.compile(
    r"\[/?(?:bold )?(?:red|green|yellow|blue|cyan)\]"
)
_LITERAL_STYLE_KWARG = re.compile(
    r'style="(?:red|green|yellow|blue|cyan|white|dim)"'
)
_TARGET_FILES = ("src/omm/cli.py", "src/omm/onboarding.py")


def test_no_literal_color_markup_or_style_kwargs():
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for relpath in _TARGET_FILES:
        text = (repo_root / relpath).read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LITERAL_MARKUP.search(line) or _LITERAL_STYLE_KWARG.search(line):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")
    assert not offenders, "Literal colors found - use a theme role instead:\n" + "\n".join(offenders)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_theme_no_hardcoded_colors.py -v`
Expected: PASS (Task 4 already eliminated every match; this test only guards against regressions going forward)

If it fails here, Task 4's migration missed a site - go fix that site now (in `cli.py`/`onboarding.py`, not this test) using the same role mapping from Task 4's script, then rerun.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q`
Expected: PASS (every test in the repo, confirming Tasks 1-8 didn't regress anything outside the files this plan touched)

- [ ] **Step 4: Commit**

```bash
git add tests/test_theme_no_hardcoded_colors.py
git commit -m "test: guard against reintroducing hardcoded terminal colors"
```
