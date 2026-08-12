# First-Run Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a genuinely fresh `omm` install, the first bare `omm` invocation shows an ASCII banner, a hardware summary, and a checklist of local AI runners the user can install — Ollama gets a real, streamed, silent auto-install; the other six engines link out to the compatibility wiki behind the same interface so they can be filled in later without touching the wizard UI.

**Architecture:** A new `src/omm/onboarding.py` module owns the wizard's UI (banner, hardware table, engine checklist, per-engine install dispatch). `linker.py` gains a generic `install_engine(key)` entry point (mirrors the existing `is_engine_installed(key)` if/elif dispatch style) with only the `"ollama"` branch implemented. `cli.py` wires the wizard into the bare `omm` invocation, gated by a `config["onboarding_completed"]` flag, plus a new `omm setup` command to re-run it on demand.

**Tech Stack:** Python, Typer, Rich (`Console`, `Table`), questionary, subprocess (streamed `Popen`), pytest + `typer.testing.CliRunner`, existing `isolated_omm_home` / `_no_real_engine_writes` fixtures from `tests/conftest.py`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-first-run-onboarding-wizard-design.md` (committed 0c8ca84).
- Migration safety is non-negotiable: existing users upgrading must never see the wizard forced on them. `DEFAULT_CONFIG["onboarding_completed"]` defaults to `True`; only the "config file didn't exist yet" branch of `config.load_config()` writes `False`.
- The wizard never runs when `stdin` is not a TTY (CI, pipes, scripted invocations) — it silently skips and leaves the flag `False` for the next interactive run.
- Only Ollama gets a real automated installer in this plan. The other six engines (`lmstudio`, `jan`, `anythingllm`, `mstystudio`, `textgenwebui`, `koboldcpp`) must appear in the checklist as "not yet automated" with a link to `COMPATIBLE_PROGRAMS_URL`, and must never call `linker.install_engine()` for those keys (it raises `NotImplementedError` for anything but `"ollama"`).
- No telemetry/upload prompt in the wizard. That stays exclusively in `omm setting telemetry` / `omm setting upload`.
- Follow existing repo conventions: `linker.py` dispatches engine-specific behavior via if/elif on a string key (not a dict of function refs) so tests can monkeypatch individual functions — see `is_engine_installed`. Tests that touch `platform.system` monkeypatch `linker.platform.system` directly (see `tests/test_linker_new_engines.py`), since `linker.py` imports `platform`/`subprocess`/`shutil` as modules, not individual names.
- All new engine-installer tests must mock `subprocess.Popen` — no real `curl`/`winget` execution in the test suite.

---

### Task 1: Config — migration-safe `onboarding_completed` flag

**Files:**
- Modify: `src/omm/config.py:38-54` (`DEFAULT_CONFIG`), `src/omm/config.py:81-94` (`load_config`)
- Test: Create `tests/test_config_onboarding.py`

**Interfaces:**
- Produces: `config.DEFAULT_CONFIG["onboarding_completed"] == True`; `config.load_config()` returns a dict whose `onboarding_completed` key is `False` only on a genuinely fresh install (no prior `config.json` on disk), `True` in every other case (existing config file, regardless of whether it already has the key).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_onboarding.py
from omm import config


def test_fresh_install_starts_onboarding_incomplete(isolated_omm_home):
    cfg = config.load_config()

    assert cfg["onboarding_completed"] is False


def test_existing_config_missing_key_defaults_to_completed(isolated_omm_home):
    config.CONFIG_PATH.write_text("{}\n")

    cfg = config.load_config()

    assert cfg["onboarding_completed"] is True


def test_existing_config_with_other_keys_defaults_to_completed(isolated_omm_home):
    config.CONFIG_PATH.write_text('{"update_channel": "beta"}\n')

    cfg = config.load_config()

    assert cfg["onboarding_completed"] is True
    assert cfg["update_channel"] == "beta"


def test_marking_onboarding_complete_persists(isolated_omm_home):
    config.load_config()

    config.update_config(onboarding_completed=True)

    assert config.load_config()["onboarding_completed"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_onboarding.py -v`
Expected: `test_fresh_install_starts_onboarding_incomplete` FAILs with `KeyError: 'onboarding_completed'` (or the value is missing/wrong); the other three currently pass by accident (there's no such key yet) but will be meaningful once Step 3 lands.

- [ ] **Step 3: Add the flag to `DEFAULT_CONFIG` and the fresh-install branch**

In `src/omm/config.py`, add the key to `DEFAULT_CONFIG` (around line 53, after `"update_channel": "stable",`):

```python
    "update_channel": "stable",
    "onboarding_completed": True,
}
```

Then change `load_config()`'s fresh-install branch (lines 83-85) so only a *genuinely new* install starts with the flag `False`:

```python
def load_config() -> dict[str, Any]:
    ensure_omm_home()
    if not CONFIG_PATH.exists():
        fresh = {**DEFAULT_CONFIG, "onboarding_completed": False}
        save_config(fresh)
        return fresh
    try:
```

(Everything from `try:` onward is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_onboarding.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full config test suite to check for regressions**

Run: `pytest tests/test_config_omm_home_env.py tests/test_cli_setting.py -v`
Expected: PASS (no changes to existing config behavior other than the new key)

- [ ] **Step 6: Commit**

```bash
git add src/omm/config.py tests/test_config_onboarding.py
git commit -m "feat: add migration-safe onboarding_completed config flag"
```

---

### Task 2: `linker.install_engine()` — Ollama auto-install

**Files:**
- Modify: `src/omm/linker.py` (add near `is_engine_installed`, around line 1500)
- Test: Create `tests/test_linker_install_engine.py`

**Interfaces:**
- Consumes: `linker.ENGINES` (`list[EngineSpec]`), `linker.is_ollama_installed() -> bool` (all pre-existing, `src/omm/linker.py:122`)
- Produces:
  - `linker.EngineInstallResult` — frozen dataclass with fields `key: str`, `status: str` (one of `"installed"`, `"failed"`, `"unsupported_platform"`), `message: str`
  - `linker.install_engine(key: str, *, on_output: Callable[[str], None] | None = None) -> EngineInstallResult` — raises `NotImplementedError` for any `key` other than `"ollama"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_linker_install_engine.py
import pytest

from omm import linker


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_engine_raises_for_unimplemented_engine():
    with pytest.raises(NotImplementedError):
        linker.install_engine("lmstudio")


def test_install_ollama_mac_streams_output_and_reports_installed(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(
        linker.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(["downloading...\n", "done\n"]),
    )
    captured = []

    result = linker.install_engine("ollama", on_output=captured.append)

    assert result == linker.EngineInstallResult(
        "ollama", "installed", "Ollama installed successfully."
    )
    assert captured == ["downloading...", "done"]


def test_install_ollama_linux_reports_failed_when_still_not_detected(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    monkeypatch.setattr(linker.subprocess, "Popen", lambda *a, **k: _FakeProc([]))

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert result.key == "ollama"


def test_install_ollama_handles_popen_start_failure(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")

    def _raise(*a, **k):
        raise OSError("no /bin/sh")

    monkeypatch.setattr(linker.subprocess, "Popen", _raise)

    result = linker.install_engine("ollama")

    assert result.status == "failed"
    assert "no /bin/sh" in result.message


def test_install_ollama_windows_without_winget_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)

    result = linker.install_engine("ollama")

    assert result.status == "unsupported_platform"


def test_install_ollama_windows_with_winget_runs_winget_install(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        linker.shutil, "which", lambda name: "C:\\winget.exe" if name == "winget" else None
    )
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc([])

    monkeypatch.setattr(linker.subprocess, "Popen", fake_popen)

    result = linker.install_engine("ollama")

    assert result.status == "installed"
    assert calls[0][:3] == ["winget", "install", "-e"]


def test_install_ollama_unknown_platform_is_unsupported(monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "FreeBSD")

    result = linker.install_engine("ollama")

    assert result.status == "unsupported_platform"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: FAIL with `AttributeError: module 'omm.linker' has no attribute 'install_engine'` (and `EngineInstallResult`)

- [ ] **Step 3: Implement `EngineInstallResult` and `install_engine()`**

In `src/omm/linker.py`, add after `is_engine_installed` (after line 1500, before `_engine_storage_dir`):

```python
@dataclass(frozen=True)
class EngineInstallResult:
    key: str
    status: str  # "installed" | "failed" | "unsupported_platform"
    message: str


def install_engine(
    key: str, *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    """Dispatch table mirroring is_engine_installed()'s if/elif style so
    individual branches stay monkeypatchable in tests. Only "ollama" has an
    automated installer today; the rest raise until a follow-up PR adds
    them one at a time behind this same interface."""
    if key == "ollama":
        return _install_ollama(on_output=on_output)
    raise NotImplementedError(f"no automated installer for engine: {key}")


def _stream_subprocess(
    args: list[str], on_output: Callable[[str], None] | None
) -> tuple[int, str] | None:
    """Runs args, streaming each stdout line to on_output as it arrives.
    Returns (returncode, None-marker) via the process wait(), or None if
    the process itself couldn't start (caller turns that into a result)."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError:
        raise
    for line in proc.stdout:
        if on_output is not None:
            on_output(line.rstrip("\n"))
    return proc.wait()


def _install_ollama(
    *, on_output: Callable[[str], None] | None = None
) -> EngineInstallResult:
    system = platform.system()
    if system in ("Darwin", "Linux"):
        try:
            _stream_subprocess(
                ["/bin/sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("ollama", "failed", f"Could not start installer: {e}")
    elif system == "Windows":
        if shutil.which("winget") is None:
            return EngineInstallResult(
                "ollama",
                "unsupported_platform",
                "winget not found - install Ollama manually from https://ollama.com/download",
            )
        try:
            _stream_subprocess(
                ["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent"],
                on_output,
            )
        except OSError as e:
            return EngineInstallResult("ollama", "failed", f"Could not start installer: {e}")
    else:
        return EngineInstallResult(
            "ollama", "unsupported_platform", f"No automated installer for {system}."
        )

    if is_ollama_installed():
        return EngineInstallResult("ollama", "installed", "Ollama installed successfully.")
    return EngineInstallResult(
        "ollama", "failed", "Installer ran but Ollama still isn't detected."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_linker_install_engine.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full linker test suite to check for regressions**

Run: `pytest tests/test_linker_new_engines.py tests/test_linker.py -v` (adjust filenames to whatever exists — run `pytest tests/ -k linker -v` if unsure)
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/omm/linker.py tests/test_linker_install_engine.py
git commit -m "feat: add linker.install_engine() with an Ollama auto-installer"
```

---

### Task 3: `onboarding.py` — banner, hardware summary, engine checklist, install orchestration

**Files:**
- Create: `src/omm/onboarding.py`
- Test: Create `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `linker.ENGINES`, `linker.is_engine_installed(key) -> bool`, `linker.install_engine(key, on_output=...) -> EngineInstallResult` (Task 2), `hardware.scan_hardware() -> HardwareInfo`, `hardware.calculate_memory_budget(hw) -> MemoryBudget` (both pre-existing, `src/omm/hardware.py`)
- Produces:
  - `onboarding.print_banner(console: Console) -> None`
  - `onboarding.print_hardware_summary(console: Console) -> None`
  - `onboarding._engine_choices() -> list[tuple[str, str]]` — `(engine_key, display_label)` pairs for engines not currently installed
  - `onboarding.run_engine_checklist(console: Console) -> list[str]` — selected engine keys
  - `onboarding._install_selected_engines(console: Console, selected: list[str]) -> None`
  - `onboarding.run_wizard(console: Console) -> None` — top-level entry point Task 4 calls

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_onboarding.py
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
    assert "\u2588" not in output  # U+2588 FULL BLOCK, only in the big art


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_onboarding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omm.onboarding'`

- [ ] **Step 3: Create `src/omm/onboarding.py`**

```python
"""First-run setup wizard: ASCII banner, hardware summary, engine checklist,
and (for now) Ollama's automated install. Every other engine links out to
the compatibility wiki behind the same _AUTOMATED_ENGINES gate, so adding
automation for one later is a one-line change here plus a new branch in
linker.install_engine() - the wizard flow itself doesn't change."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from omm import linker
from omm.hardware import calculate_memory_budget, scan_hardware

COMPATIBLE_PROGRAMS_URL = "https://github.com/omm-hippo/omm/wiki/Compatible-Programs"

_AUTOMATED_ENGINES = {"ollama"}

_ASCII_ART = r"""
 ██████╗ ███╗   ███╗███╗   ███╗
██╔═══██╗████╗ ████║████╗ ████║
██║   ██║██╔████╔██║██╔████╔██║
██║   ██║██║╚██╔╝██║██║╚██╔╝██║
╚██████╔╝██║ ╚═╝ ██║██║ ╚═╝ ██║
 ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═╝
""".strip("\n")

_ASCII_ART_WIDTH = max(len(line) for line in _ASCII_ART.splitlines())


def print_banner(console: Console) -> None:
    if console.size.width >= _ASCII_ART_WIDTH:
        console.print(f"[bold cyan]{_ASCII_ART}[/bold cyan]")
    else:
        console.print("[bold cyan]omm[/bold cyan] - local LLM package manager")
    console.print("[dim]Let's get you set up.[/dim]\n")


def print_hardware_summary(console: Console) -> None:
    info = scan_hardware()
    budget = calculate_memory_budget(info)

    table = Table(title="Your machine", box=None)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    table.add_row("Safe model budget", f"{budget.model_budget_gb:.1f} GB")
    if info.gpu_name:
        table.add_row("GPU", info.gpu_name)
    console.print(table)
    console.print()


def _engine_choices() -> list[tuple[str, str]]:
    """(key, label) pairs for engines not yet detected on this machine."""
    choices = []
    for spec in linker.ENGINES:
        if linker.is_engine_installed(spec.key):
            continue
        tag = (
            "auto-install"
            if spec.key in _AUTOMATED_ENGINES
            else "not yet automated - see compatibility wiki"
        )
        choices.append((spec.key, f"{spec.label}  ({tag})"))
    return choices


def run_engine_checklist(console: Console) -> list[str]:
    import questionary

    choices = _engine_choices()
    if not choices:
        console.print("[dim]All known local AI runners are already installed.[/dim]\n")
        return []

    selected = questionary.checkbox(
        "Install any local AI runners you'd like to use? (space to select, enter to confirm)",
        choices=[questionary.Choice(title=label, value=key) for key, label in choices],
    ).ask()
    return selected or []


def _install_selected_engines(console: Console, selected: list[str]) -> None:
    specs_by_key = {spec.key: spec for spec in linker.ENGINES}
    for key in selected:
        spec = specs_by_key[key]
        if key not in _AUTOMATED_ENGINES:
            console.print(
                f"[yellow]{spec.label} isn't auto-installable yet.[/yellow] "
                f"See {COMPATIBLE_PROGRAMS_URL}"
            )
            continue
        console.print(f"\n[bold]Installing {spec.label}...[/bold]")
        result = linker.install_engine(
            key, on_output=lambda line: console.print(f"[dim]{line}[/dim]")
        )
        style = "green" if result.status == "installed" else "red"
        console.print(f"[{style}]{result.message}[/{style}]")


def run_wizard(console: Console) -> None:
    print_banner(console)
    print_hardware_summary(console)
    selected = run_engine_checklist(console)
    if selected:
        _install_selected_engines(console, selected)
    console.print(
        "\n[bold green]Setup complete.[/bold green] "
        "Run `omm setting` any time to change telemetry, upload, or update-channel settings.\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_onboarding.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/omm/onboarding.py tests/test_onboarding.py
git commit -m "feat: add onboarding wizard module (banner, hardware summary, engine checklist)"
```

---

### Task 4: Wire the wizard into `cli.py` + `omm setup` command

**Files:**
- Modify: `src/omm/cli.py` (import block ~line 33-53, `_ROOT_HELP_TEXT` ~line 112-133, `_root` ~line 232-244, add `setup_cmd` after `scan()` ~line 414)
- Test: Create `tests/test_cli_onboarding.py`

**Interfaces:**
- Consumes: `onboarding.run_wizard(console: Console) -> None` (Task 3), `config_mod.update_config(**changes) -> dict`, `load_config() -> dict`, `_stdin_is_tty() -> bool` (pre-existing, `src/omm/cli.py:944`)
- Produces: `omm setup` CLI command; bare `omm` runs the wizard once per fresh install before printing its usual version banner

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_onboarding.py
from typer.testing import CliRunner

from omm import cli, config, onboarding

runner = CliRunner()


def test_bare_omm_runs_wizard_once_on_fresh_tty_install(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert config.load_config()["onboarding_completed"] is True


def test_bare_omm_skips_wizard_when_not_a_tty(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0, result.stdout
    assert calls == []
    assert config.load_config()["onboarding_completed"] is False


def test_bare_omm_skips_wizard_when_already_completed(isolated_omm_home, monkeypatch):
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    runner.invoke(cli.app, [])

    assert calls == []


def test_setup_command_reruns_wizard_and_marks_completed(isolated_omm_home, monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding, "run_wizard", lambda console: calls.append(console))

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert config.load_config()["onboarding_completed"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_onboarding.py -v`
Expected: FAIL — `test_bare_omm_runs_wizard_once_on_fresh_tty_install` and `test_setup_command_reruns_wizard_and_marks_completed` fail because nothing calls `onboarding.run_wizard` yet and there's no `setup` command (`Error: No such command 'setup'`).

- [ ] **Step 3: Import `onboarding` and add `_maybe_run_onboarding()`**

In `src/omm/cli.py`, add `onboarding` to the `from omm import (...)` block (it's alphabetically between `linker` and `predictor`, around line 41):

```python
from omm import (
    benchmark,
    benchmark_history,
    calibration,
    catalog,
    config as config_mod,
    contribute_state,
    linker,
    onboarding,
    predictor,
```

Add `_maybe_run_onboarding()` near the other `_maybe_*` helpers (find `_maybe_start_update_check` with `grep -n "_maybe_start_update_check" src/omm/cli.py` and place this right before it):

```python
def _maybe_run_onboarding() -> None:
    """Runs the first-time setup wizard exactly once, only for a genuinely
    fresh install (see config.load_config()'s migration handling) and only
    when there's a real terminal to drive questionary's checklist."""
    if load_config().get("onboarding_completed", True):
        return
    if not _stdin_is_tty():
        return
    onboarding.run_wizard(console)
    config_mod.update_config(onboarding_completed=True)
```

- [ ] **Step 4: Call it from `_root()`**

Modify `src/omm/cli.py:232-238`:

```python
@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        _maybe_run_onboarding()
        console.print(f"omm {_version_line(_installed_commit())}")
        console.print(f"[dim]{_telemetry_destination_line()}[/dim]")
        raise typer.Exit(0)
```

- [ ] **Step 5: Add the `omm setup` command**

Add after the `scan()` command (after its closing lines, around line 414, before `def _refresh_data():`):

```python
@app.command(name="setup")
def setup_cmd() -> None:
    """Re-run the first-time setup wizard (hardware scan + engine checklist)."""
    onboarding.run_wizard(console)
    config_mod.update_config(onboarding_completed=True)
```

- [ ] **Step 6: Add `omm setup` to the curated help text**

Modify `src/omm/cli.py:124-127`:

```python
Maintenance:
  omm scan
  omm setup
  omm upgrade [MODEL]
  omm setting
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_cli_onboarding.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Run the full test suite to check for regressions**

Run: `pytest tests/ -v`
Expected: PASS. Pay special attention to `tests/test_cli_help_version.py` (bare-`omm` output shape) and `tests/test_cli_help_version.py::test_help_all_lists_every_command` (new `setup` command doesn't break the `--all` listing).

- [ ] **Step 9: Commit**

```bash
git add src/omm/cli.py tests/test_cli_onboarding.py
git commit -m "feat: trigger onboarding wizard on first bare omm run, add omm setup"
```

---

## Follow-up (not part of this plan)

- Automating LM Studio, Jan, AnythingLLM, Msty, text-generation-webui, and KoboldCpp: each is a new `elif key == "..."` branch in `linker.install_engine()` plus removing that key from `onboarding._AUTOMATED_ENGINES`'s exclusion (i.e. adding it to the set) — no wizard UI changes needed. Track as follow-up issues off #24.
- Verifying the Windows `winget` package id `Ollama.Ollama` against a real Windows machine — Task 2 ships with this id unverified per the spec's open note.
