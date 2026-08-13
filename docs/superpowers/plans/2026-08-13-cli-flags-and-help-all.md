# CLI Flags Expansion + `help --all` Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give omm's CLI global flags (`--json`/`--yes`/`--quiet`/`--no-color`), command aliases (`rm`/`ls`/`up`), a handful of missing per-command control flags, and a `help --all` that actually shows every command (including nested `setting` subcommands) with their full options.

**Architecture:** A `GlobalOptions` dataclass lives on the root Click context (`ctx.obj`). A `global_flags` decorator injects the four global options onto every relevant `@app.command()` function by rewriting its `inspect.Signature` (so Typer registers them as real per-command Click options, making them valid *after* the subcommand name), while the root callback (`_root`) also declares them so they're valid *before* the subcommand name. Both write into the same `ctx.obj`, so either position works and a value given after the subcommand always wins if present. `help --all` replaces its single `Group.format_help()` call with a small recursive renderer reusing each command's own `get_help()`.

**Tech Stack:** Python 3.11+, Typer 0.27 (vendors its own Click fork at `typer._click`), Click 8.4.2, Rich 15 (console output), pytest + `typer.testing.CliRunner`.

**Spec:** `docs/superpowers/specs/2026-08-13-cli-flags-and-help-all-design.md`

## Global Constraints

- All command-line entry points live in `src/omm/cli.py` (single file, ~4080 lines) — follow its existing style (Rich `console`/`err_console`, `typer.Exit`, `err_console.print(f"[red]...[/red]")` for errors).
- `cli.py` has `from __future__ import annotations` at the top (line 3) — every new function must stay compatible with that (string-form annotations at parse time). The `global_flags` decorator MUST call `inspect.signature(func, eval_str=True)`, not bare `inspect.signature(func)`, or the carried-over original parameters keep unresolved string annotations and Typer's option-building breaks.
- Typer 0.27 vendors its own Click fork at `typer._click`. Context state must be read via `typer._click.globals.get_current_context()` — the top-level `click.get_current_context()` reads a *different*, unrelated thread-local stack and will raise `RuntimeError: There is no active click context.` even mid-invocation. (`cli.py` already patches `typer._click.core.Context.formatter_class` separately from `click.Context.formatter_class` for this exact reason — same fork split.)
- Global flag help text is generic and identical across every command (e.g. `--yes`/`-y` always reads "Skip confirmation prompts. For scripting."). Don't preserve each command's previous bespoke wording — consistency across commands is the point of this plan.
- Every new/changed CLI behavior needs a test using `typer.testing.CliRunner` against `cli.app`, following the existing patterns in `tests/test_cli_*.py`. Use the `isolated_omm_home` fixture (`tests/conftest.py:35`) whenever a test touches the registry/config/models dir.
- Run the full suite (`pytest`) before the final commit of each task — this file is large and shared by every command, so a regression in one task can silently break another.

---

### Task 1: `GlobalOptions` + `global_flags` decorator + root wiring (pilot: `scan`)

**Files:**
- Modify: `src/omm/cli.py` (imports near line 3-31; new code block after `PlainHelpFormatter`/before `_ROOT_HELP_TEXT`, i.e. around line 100; `_root` at line 234-247; `scan` at line 340-341)
- Test: `tests/test_cli_global_flags.py` (new)

**Interfaces:**
- Produces: `GlobalOptions` dataclass (`json: bool`, `yes: bool`, `quiet: bool`, `no_color: bool`, `pending_telemetry_notice: int`), `global_flags(func)` decorator, `_global_opts() -> GlobalOptions` helper. All later tasks import/use these three names from `cli.py` (they live in the same module, no import needed — just reuse).

- [ ] **Step 1: Add the new imports**

At the top of `src/omm/cli.py`, add to the stdlib import block (after `import errno` at line 5):

```python
import functools
import inspect
```

And add to the existing `from typing import ...`-style area — `cli.py` doesn't currently import from `typing` at module level, so add a new line right after the `from pathlib import Path` line (line 17):

```python
from typing import Annotated
```

- [ ] **Step 2: Write the `GlobalOptions` dataclass and `global_flags` decorator**

Insert this new block in `src/omm/cli.py` right after the `PlainHelpFormatter` class and its `click.Context.formatter_class` / `typer._click.core.Context.formatter_class` patching (i.e. right before the `_ROOT_HELP_TEXT = """..."""` assignment, around line 103):

```python
@dataclass
class GlobalOptions:
    """Merged state for the 4 global flags, shared via ctx.obj. A value
    given after the subcommand name always overrides one given before it;
    see global_flags()."""

    json: bool = False
    yes: bool = False
    quiet: bool = False
    no_color: bool = False
    pending_telemetry_notice: int = 0


def _global_opts() -> GlobalOptions:
    """Read the merged GlobalOptions for the command currently running.
    Only valid while a Click/Typer command is executing."""
    from typer._click.globals import get_current_context

    return get_current_context().ensure_object(GlobalOptions)


def global_flags(func):
    """Attach --json/--yes/--quiet/--no-color to a command so they also
    work positioned after the subcommand name (the root callback already
    covers positioning before it). Rewrites the wrapped function's
    inspect.Signature so Typer registers 4 extra Click options without
    every command function having to redeclare them. Values merge into
    the same GlobalOptions ctx.obj the root callback populated; a value
    given here (post-subcommand) always wins over one given before the
    subcommand name, since this wrapper runs after the root callback."""
    original_sig = inspect.signature(func, eval_str=True)
    new_params = list(original_sig.parameters.values()) + [
        inspect.Parameter(
            "json_flag",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[
                bool, typer.Option("--json", help="Print output as JSON where supported.")
            ],
        ),
        inspect.Parameter(
            "yes_flag",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[
                bool,
                typer.Option("--yes", "-y", help="Skip confirmation prompts. For scripting."),
            ],
        ),
        inspect.Parameter(
            "quiet_flag",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[
                bool,
                typer.Option(
                    "--quiet", "-q", help="Suppress banners and informational output."
                ),
            ],
        ),
        inspect.Parameter(
            "no_color_flag",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[bool, typer.Option("--no-color", help="Disable colored output.")],
        ),
    ]

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from typer._click.globals import get_current_context

        ctx = get_current_context()
        opts = ctx.ensure_object(GlobalOptions)
        if kwargs.pop("json_flag", False):
            opts.json = True
        if kwargs.pop("yes_flag", False):
            opts.yes = True
        if kwargs.pop("quiet_flag", False):
            opts.quiet = True
        if kwargs.pop("no_color_flag", False):
            opts.no_color = True
        if opts.no_color:
            console.no_color = True
            err_console.no_color = True
        if opts.pending_telemetry_notice and not (opts.json or opts.quiet):
            console.print(
                f"[dim]Sent {opts.pending_telemetry_notice} queued telemetry "
                "event(s) from a previous session.[/dim]"
            )
        opts.pending_telemetry_notice = 0
        return func(*args, **kwargs)

    wrapper.__signature__ = original_sig.replace(parameters=new_params)
    return wrapper
```

Note: `console`/`err_console` aren't defined yet at this point in the file (they're created later, around line 168-169). That's fine — the decorator's `wrapper` body only *references* them at call time, long after module load finishes, so forward-reference is safe here (same reason the rest of the file's helper functions can reference `console` before its definition line).

- [ ] **Step 3: Wire the root callback to declare the flags (pre-subcommand position) and stop printing the telemetry notice directly**

In `_root` (`cli.py:234-247`), replace:

```python
@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        _maybe_run_onboarding()
        console.print(f"omm {_version_line(_installed_commit())}")
        console.print(f"[dim]{_telemetry_destination_line()}[/dim]")
        raise typer.Exit(0)
    _maybe_auto_import(ctx)
    resent = telemetry.flush_pending()
    if resent:
        console.print(
            f"[dim]Sent {resent} queued telemetry event(s) from a previous session.[/dim]"
        )
```

with:

```python
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    json_flag: Annotated[bool, typer.Option("--json", help="Print output as JSON where supported.")] = False,
    yes_flag: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts. For scripting.")] = False,
    quiet_flag: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress banners and informational output.")] = False,
    no_color_flag: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
) -> None:
    opts = ctx.ensure_object(GlobalOptions)
    opts.json = opts.json or json_flag
    opts.yes = opts.yes or yes_flag
    opts.quiet = opts.quiet or quiet_flag
    opts.no_color = opts.no_color or no_color_flag
    if opts.no_color:
        console.no_color = True
        err_console.no_color = True
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        _maybe_run_onboarding()
        console.print(f"omm {_version_line(_installed_commit())}")
        console.print(f"[dim]{_telemetry_destination_line()}[/dim]")
        raise typer.Exit(0)
    _maybe_auto_import(ctx)
    opts.pending_telemetry_notice = telemetry.flush_pending()
```

This moves the "resent N telemetry events" print out of `_root` (which runs *before* any post-subcommand `--json`/`--quiet` has been parsed, so printing there unconditionally would still leak into `--json` piping) and defers it to `global_flags`'s wrapper (Step 2), which runs after the full merge and can correctly suppress it under `--json`/`--quiet`, or route it to `err_console` — done here by simply not printing to stdout at all when suppressed, matching the spec's "either move to stderr or suppress" choice: suppress, since it's pure noise for scripting and re-appears next run regardless.

- [ ] **Step 4: Apply `@global_flags` to `scan` as the pilot command, and add `--json`**

`scan` (`cli.py:340-422`) currently takes no parameters and always prints Rich tables. Change:

```python
@app.command()
def scan() -> None:
```

to:

```python
@app.command()
@global_flags
def scan() -> None:
```

Then, inside the function body, gate the whole thing behind `_global_opts().json`. Replace the full body (`cli.py:342-422`, everything between the docstring and the next top-level command) so the top of the function reads:

```python
    """Scan current PC hardware (RAM, VRAM, OS) and print a summary table."""
    opts = _global_opts()
    info = scan_hardware()
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    reg = registry.load_registry()
    cleaned = _reconcile_stale_link_records(reg, installed)
    external = scan_import.find_external_models()

    if opts.json:
        console.print_json(
            data={
                "os": f"{info.os_name} {info.os_version}",
                "cpu": info.cpu,
                "ram_total_gb": info.ram_total_gb,
                "ram_available_gb": info.ram_available_gb,
                "model_budget_gb": calculate_memory_budget(info).model_budget_gb,
                "gpu_name": info.gpu_name,
                "unified_memory": info.unified_memory,
                "vram_total_gb": info.vram_total_gb,
                "vram_free_gb": info.vram_free_gb,
                "engines_installed": [spec.key for spec in linker.ENGINES if installed[spec.key]],
                "models": [
                    {
                        "filename": filename,
                        "location": "hub",
                        "engines": [name for name, on in entry.get("linked", {}).items() if on],
                        "managed_by_omm": True,
                    }
                    for filename, entry in reg.items()
                ]
                + [
                    {
                        "filename": item.display_name,
                        "location": str(item.path),
                        "engines": [item.engine],
                        "managed_by_omm": False,
                    }
                    for item in external
                ],
            }
        )
        return

    table = Table(title="omm hardware scan")
```

...and keep everything from the original `table = Table(title="omm hardware scan")` line onward (`cli.py:345-421` in the original) **unchanged**, just deleting the now-duplicated `installed = ...`, `reg = ...`, `cleaned = ...`, `external = ...` lines that used to appear further down in the original body (they've been hoisted above the `if opts.json:` block so both branches can use them). Concretely: after the `if opts.json: ... return` block, the non-JSON path continues exactly as the original code did from `table = Table(title="omm hardware scan")` through the final `if external:` block, **except** remove these now-redundant re-declarations that the original had inline:
- the `installed = {...}` line (originally right after `console.print(table)` for the OS table)
- the `reg = registry.load_registry()` / `cleaned = _reconcile_stale_link_records(reg, installed)` / `external = scan_import.find_external_models()` lines (originally right before the "Local AI models" table)

- [ ] **Step 5: Write the tests**

Create `tests/test_cli_global_flags.py`:

```python
from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def test_global_flag_works_before_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["--json", "scan"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("{")


def test_global_flag_works_after_subcommand(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan", "--json"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().startswith("{")


def test_scan_without_json_prints_table(isolated_omm_home):
    result = runner.invoke(cli.app, ["scan"])
    assert result.exit_code == 0, result.stdout
    assert "omm hardware scan" in result.stdout.upper() or "OMM HARDWARE SCAN" in result.stdout


def test_no_color_flag_disables_ansi_codes(isolated_omm_home):
    result = runner.invoke(cli.app, ["--no-color", "scan"])
    assert result.exit_code == 0, result.stdout
    assert "\x1b[" not in result.stdout
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_global_flags.py -v`
Expected: all 4 PASS.

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `.venv/bin/pytest -q`
Expected: no new failures versus the baseline (there may be pre-existing unrelated failures; compare against a run on the base commit if unsure).

- [ ] **Step 8: Commit**

```bash
git add src/omm/cli.py tests/test_cli_global_flags.py
git commit -m "feat: add global --json/--yes/--quiet/--no-color flags (pilot: scan)"
```

---

### Task 2: Consolidate existing `--json` on `search`/`list`/`info`/`benchmark`

**Files:**
- Modify: `src/omm/cli.py` (`search` at 2688-2786, `list_models` at 2329-2370, `info` at 2149-2209, `benchmark_cmd` at 2997-3184)
- Test: `tests/test_cli_search.py`, `tests/test_cli_list.py`, `tests/test_cli_info.py`, `tests/test_cli_benchmark.py` (extend existing files)

**Interfaces:**
- Consumes: `global_flags` decorator, `_global_opts()` from Task 1.

- [ ] **Step 1: `search` — remove local `--json`, use `_global_opts()`**

In `cli.py`, change:

```python
@app.command()
def search(
    query: str,
    json_output: bool = typer.Option(False, "--json", help="Print results as JSON instead of a table."),
    skip_unfit: bool = typer.Option(
```

to:

```python
@app.command()
@global_flags
def search(
    query: str,
    skip_unfit: bool = typer.Option(
```

Then, at the top of the function body (right after the docstring `"""Search curated models, cached candidates, and HuggingFace by name."""`), add:

```python
    json_output = _global_opts().json
```

This keeps every other reference to `json_output` in the function body (`cli.py:2762`, `2784-2785`) working unchanged — only the parameter's *source* changes.

- [ ] **Step 2: `list_models` — same pattern**

Change:

```python
@app.command(name="list")
def list_models(
    json_output: bool = typer.Option(False, "--json", help="Print results as JSON instead of a table."),
) -> None:
    """Show models installed via omm and their linked status."""
```

to:

```python
@app.command(name="list")
@global_flags
def list_models() -> None:
    """Show models installed via omm and their linked status."""
    json_output = _global_opts().json
```

(The `--engine` flag added in Task 6 will add a real parameter back to this signature — that's fine, this step just removes `json_output` as a declared parameter for now.)

- [ ] **Step 3: `info` — same pattern**

Change:

```python
@app.command()
def info(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
    json_output: bool = typer.Option(False, "--json", help="Print result as JSON instead of a table."),
) -> None:
    """Show name, version, size, and linked-program run commands for an installed model."""
```

to:

```python
@app.command()
@global_flags
def info(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
    """Show name, version, size, and linked-program run commands for an installed model."""
    json_output = _global_opts().json
```

- [ ] **Step 4: `benchmark_cmd` — same pattern, plus wire `Progress(disable=...)`**

Change the decorator/signature (`cli.py:2997-3031`) by adding `@global_flags` above `@app.command(name="benchmark")`'s function, and removing the `json_output` parameter:

```python
@app.command(name="benchmark")
@global_flags
def benchmark_cmd(
    models: list[str] = typer.Argument(
        ...,
        help="One or more already-installed Ollama tags.",
    ),
    pack: Path | None = typer.Option(
        None,
        "--pack",
        help="Use a different versioned JSON pack.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Write evidence to this JSON path.",
    ),
    speed_runs: int = typer.Option(3, "--speed-runs", min=1, max=10),
    confirm_performance_timeout: bool = typer.Option(
        False,
        "--confirm-performance-timeout",
        help=(
            "If a model's first generation attempt times out, wait for it to "
            "fully finish, health-check the daemon, and retry exactly once "
            "before deciding. Two confirmed timeouts under a healthy daemon "
            "are reported as performance_unfit instead of transient_error. "
            "Off by default: a single timeout is never auto-retried unless "
            "you pass this flag."
        ),
    ),
) -> None:
    """Measure a small reproducible quality pack and decode speed."""
    json_output = _global_opts().json
```

Then update the `Progress(` construction at `cli.py:3049-3054` to respect `--quiet`:

```python
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}[/cyan]"),
                TimeElapsedColumn(),
                console=console,
                disable=_global_opts().quiet,
            ) as progress:
```

- [ ] **Step 5: Extend the tests**

In `tests/test_cli_search.py`, `tests/test_cli_list.py`, `tests/test_cli_info.py`, `tests/test_cli_benchmark.py`, find the existing `--json` test in each (they already exist per the spec's command inventory) and add one line to each confirming the post-subcommand `--json` still works AND the pre-subcommand global form works too, e.g. for search add:

```python
def test_search_json_before_subcommand(isolated_omm_home, monkeypatch):
    # reuse whatever local/network mocking the existing `--json` test in this
    # file already sets up; just change the invoked args to:
    result = runner.invoke(cli.app, ["--json", "search", "some-query"])
    assert result.exit_code in (0, 1)  # 1 if no matches - same as the existing --json test
```

(Match this exactly to how the existing `--json` test in that file mocks `search_mod`/network calls — copy its setup, only change the argv list to lead with `--json`.)

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_search.py tests/test_cli_list.py tests/test_cli_info.py tests/test_cli_benchmark.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 8: Commit**

```bash
git add src/omm/cli.py tests/test_cli_search.py tests/test_cli_list.py tests/test_cli_info.py tests/test_cli_benchmark.py
git commit -m "refactor: consolidate search/list/info/benchmark --json onto the global flag"
```

---

### Task 3: Consolidate existing `--yes` on `import`/`uninstall`/`upgrade`/`contribute`, wire `install`

**Files:**
- Modify: `src/omm/cli.py` (`import_cmd` 679-699, `remove` 2093-2131, `upgrade` 2283-2327, `contribute` 3920-4054, `_install_impl` 1677-1687+1704-1722, `install` 1972-2033)
- Test: `tests/test_cli_import.py`, `tests/test_uninstall_smoke.py` or `tests/test_cli_remove.py`, `tests/test_cli_upgrade.py`, `tests/test_cli_contribute.py`, `tests/test_cli_install_confirm.py`

**Interfaces:**
- Consumes: `global_flags`, `_global_opts()` from Task 1.
- Produces: `_install_impl(..., assume_yes: bool = False, force: bool = False)` — later tasks (none) depend on this, but keep the two other call sites (`cli.py:3743`, `3785` inside `_run_contribution_loop`) untouched; they rely on the new params defaulting to `False`.

- [ ] **Step 1: `import_cmd`**

Change:

```python
@app.command(name="import")
def import_cmd(
    path: str = typer.Argument(
        None, help="Optional extra directory to also scan for stray .gguf files."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask for confirmation and import every model found. For scripting.",
    ),
) -> None:
    """Scan every supported local AI app (and optionally PATH) for .gguf
    files not yet managed by omm, and offer to adopt them into the hub."""
    extra_path = None
    if path:
        extra_path = Path(path).expanduser()
        if not extra_path.is_dir():
            err_console.print(f"[red]Not a directory: {extra_path}[/red]")
            raise typer.Exit(1)
    _run_import_flow(extra_path, yes=yes)
```

to:

```python
@app.command(name="import")
@global_flags
def import_cmd(
    path: str = typer.Argument(
        None, help="Optional extra directory to also scan for stray .gguf files."
    ),
) -> None:
    """Scan every supported local AI app (and optionally PATH) for .gguf
    files not yet managed by omm, and offer to adopt them into the hub."""
    extra_path = None
    if path:
        extra_path = Path(path).expanduser()
        if not extra_path.is_dir():
            err_console.print(f"[red]Not a directory: {extra_path}[/red]")
            raise typer.Exit(1)
    _run_import_flow(extra_path, yes=_global_opts().yes)
```

- [ ] **Step 2: `remove` (registered as `uninstall`)**

Change:

```python
@app.command(name="uninstall")
def remove(
    filename: str = typer.Argument(..., autocompletion=complete_remove_filename),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask for confirmation before uninstalling `all`. For scripting.",
    ),
) -> None:
```

to:

```python
@app.command(name="uninstall")
@global_flags
def remove(
    filename: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
```

And inside the body, change `if not yes and not _ask_confirm(...)` (`cli.py:2110`) to:

```python
        if not _global_opts().yes and not _ask_confirm(f"Uninstall all {len(reg)} model(s)?"):
```

- [ ] **Step 3: `upgrade`**

Change:

```python
@app.command()
def upgrade(
    model_name: str = typer.Argument(None, autocompletion=complete_remove_filename),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask for confirmation before checking all models. For scripting.",
    ),
) -> None:
```

to:

```python
@app.command()
@global_flags
def upgrade(
    model_name: str = typer.Argument(None, autocompletion=complete_remove_filename),
) -> None:
```

And `if not yes and not _ask_confirm(...)` (`cli.py:2302`) to:

```python
        if not _global_opts().yes and not _ask_confirm(f"Check {len(reg)} model(s) for updates?"):
```

- [ ] **Step 4: `contribute`**

Change:

```python
@app.command()
def contribute(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Don't ask for confirmation before starting. For scripting/unattended runs.",
    ),
) -> None:
    """Repeatedly install, benchmark, and upload telemetry for hardware-fit
    models until Esc is pressed, to help grow the training dataset behind
    `omm recommend`. Deletes each model after benchmarking it (even
    successful ones) to keep disk usage bounded."""
    policy = load_config().get("telemetry_send_policy", "ask")
```

to:

```python
@app.command()
@global_flags
def contribute() -> None:
    """Repeatedly install, benchmark, and upload telemetry for hardware-fit
    models until Esc is pressed, to help grow the training dataset behind
    `omm recommend`. Deletes each model after benchmarking it (even
    successful ones) to keep disk usage bounded."""
    yes = _global_opts().yes
    policy = load_config().get("telemetry_send_policy", "ask")
```

Leave the three existing internal uses of `yes` (`cli.py:3944`, `3951`, `3976` in the original numbering) exactly as they are — they now read the local `yes` variable assigned from `_global_opts().yes` above, so no further edits are needed there.

- [ ] **Step 5: `_install_impl` — add `assume_yes` and `force` params**

Change the signature (`cli.py:1677-1687`):

```python
def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    no_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
    use_quality_eval: bool = False,
    quality_pack: dict | None = None,
    link_only_ollama: bool = False,
) -> InstallOutcome:
```

to:

```python
def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    no_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
    use_quality_eval: bool = False,
    quality_pack: dict | None = None,
    link_only_ollama: bool = False,
    assume_yes: bool = False,
    force: bool = False,
) -> InstallOutcome:
```

Then in the body, change (`cli.py:1706-1708`):

```python
            if not _ask_confirm("Install anyway?"):
                err_console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)
```

to:

```python
            if not assume_yes and not _ask_confirm("Install anyway?"):
                err_console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)
```

And change (`cli.py:1721-1722`):

```python
    if dest.exists():
        err_console.print(f"[yellow]{filename} already downloaded, skipping fetch.[/yellow]")
```

to:

```python
    if dest.exists() and not force:
        err_console.print(f"[yellow]{filename} already downloaded, skipping fetch.[/yellow]")
```

- [ ] **Step 6: `install` — apply `@global_flags`, add `--force`, thread both through**

Change the signature (`cli.py:1972-1988`):

```python
@app.command()
def install(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
    skip_unfit: bool = typer.Option(
        False,
        "--skip-unfit",
        help="If this hardware is predicted not to run the model, skip it "
        "instead of asking (exits 0 with skipped_unfit set). For scripting.",
    ),
    upload: bool | None = typer.Option(
        None,
        "--upload/--no-upload",
        help="Send (or skip sending) this machine's benchmark result to the "
        "telemetry server, without asking. Unset defers to the current "
        "`omm setting upload` policy.",
    ),
) -> None:
    """Download a model into the central hub and link it into installed engines."""
    import questionary

    model_name = _resolve_ref(model_name)
```

to:

```python
@app.command()
@global_flags
def install(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
    skip_unfit: bool = typer.Option(
        False,
        "--skip-unfit",
        help="If this hardware is predicted not to run the model, skip it "
        "instead of asking (exits 0 with skipped_unfit set). For scripting.",
    ),
    upload: bool | None = typer.Option(
        None,
        "--upload/--no-upload",
        help="Send (or skip sending) this machine's benchmark result to the "
        "telemetry server, without asking. Unset defers to the current "
        "`omm setting upload` policy.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-download even if this model is already installed.",
    ),
) -> None:
    """Download a model into the central hub and link it into installed engines."""
    import questionary

    model_name = _resolve_ref(model_name)
```

Then update the three recursive self-calls and the `_install_impl` call within the same function (`cli.py:2000`, `2012`, `2019-2024`):

```python
        install(f"{e.provider}:{e.repo_id}:{chosen}", skip_unfit=skip_unfit, upload=upload, force=force)
```

```python
        install(f"{chosen_provider}:{e.repo_id}", skip_unfit=skip_unfit, upload=upload, force=force)
```

```python
    outcome = _install_impl(
        resolved,
        skip_unfit=skip_unfit,
        auto_upload=upload is True,
        no_upload=upload is False,
        assume_yes=_global_opts().yes,
        force=force,
    )
```

- [ ] **Step 7: Extend the tests**

- `tests/test_cli_import.py`: add a case invoking `["import", "some/path", "--yes"]` (or whatever the existing `--yes` test already covers) and confirm it still passes; add one with `["--yes", "import", "some/path"]`.
- `tests/test_cli_remove.py` / `tests/test_uninstall_smoke.py`: same — one test with `-y` before `uninstall all`, one after.
- `tests/test_cli_upgrade.py`: same pattern for `upgrade --yes` / `--yes upgrade`.
- `tests/test_cli_contribute.py`: same pattern for `contribute --yes` / `--yes contribute` (mock whatever the existing contribute tests already mock — daemon/network — don't add new mocking infrastructure).
- `tests/test_cli_install_confirm.py` (or wherever `install`'s "Install anyway?" prompt is currently tested): add a case that predicts the hardware as unfit (however the existing test mocks that), passes `--yes`, and asserts no interactive prompt was needed (no `_ask_confirm` call, or exit code 0 without stdin interaction).
- New file or extend an existing install test: a case that installs a model, then calls `install` again with `--force` and asserts `download_file`/`remote_file_size` mocks were invoked again (as opposed to the "already downloaded, skipping fetch" message appearing).

Write these using the exact mocking patterns already present in each target file — do not invent new fixtures.

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_import.py tests/test_cli_remove.py tests/test_uninstall_smoke.py tests/test_cli_upgrade.py tests/test_cli_contribute.py tests/test_cli_install_confirm.py -v`
Expected: all PASS.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 10: Commit**

```bash
git add src/omm/cli.py tests/test_cli_import.py tests/test_cli_remove.py tests/test_uninstall_smoke.py tests/test_cli_upgrade.py tests/test_cli_contribute.py tests/test_cli_install_confirm.py
git commit -m "feat: consolidate --yes onto the global flag, add install --force"
```

---

### Task 4: Apply `@global_flags` to the remaining commands + `tune --json`

**Files:**
- Modify: `src/omm/cli.py` — `setup_cmd` (425), `update` (929), `recommend` (1200), `tune` (1280), `link_models` (2813, decorator only — `--engine` comes in Task 6), `autoremove` (2969, decorator only — no new flags, see Task 6), and the 7 `setting_app` commands: `configure_telemetry` (2374), `configure_upload` (2408), `configure_version` (2440), `calibrate` (2475), `catalog_trust` (2543), `catalog_status` (2564), `catalog_rollback` (2584). Also the `Progress(` at `cli.py:774` inside `_run_pipx_install_with_progress` (used by `update`).
- Test: `tests/test_cli_memory_and_tune.py` (tune --json), `tests/test_cli_setting.py`, `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `global_flags`, `_global_opts()` from Task 1.

- [ ] **Step 1: Add `@global_flags` to the 9 flag-free commands**

For each of `setup_cmd`, `update`, `recommend`, `link_models`, `autoremove`, `configure_telemetry`, `configure_upload`, `configure_version`, `calibrate`, `catalog_trust`, `catalog_status`, `catalog_rollback`, add a `@global_flags` line directly under its existing `@app.command(...)` / `@setting_app.command(...)` decorator. None of these need body changes in this task (their own specific options are untouched) — this step is purely adding the decorator line 11 times, once per function listed above.

Example for `catalog_status` (`cli.py:2563-2564`):

```python
@setting_app.command(name="catalog-status")
@global_flags
def catalog_status() -> None:
```

- [ ] **Step 2: Wire `--quiet` into `update`'s progress bar**

In `_run_pipx_install_with_progress` (`cli.py:773-787`), the `Progress(` construction at line 774 doesn't currently take a `console=` kwarg — check its exact current arguments before editing (read `cli.py:773-790`) and add `disable=_global_opts().quiet` to its keyword arguments, matching how Task 2 Step 4 did it for `benchmark_cmd`. If this helper is called from a place with no active Click context (verify by checking `_migrate_to_editable_install`'s call site), guard with a try/except around `_global_opts()` defaulting to `False` — but first check: `_run_pipx_install_with_progress` is only ever called from `update()`, which is itself a Typer command, so a Click context is always active. No guard needed; call `_global_opts().quiet` directly.

- [ ] **Step 3: `tune` — add `--json`**

Change (`cli.py:1279-1283`):

```python
@app.command()
def tune(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
) -> None:
    """Recommend context, GPU offload, threads, and batch size for a model."""
```

to:

```python
@app.command()
@global_flags
def tune(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
) -> None:
    """Recommend context, GPU offload, threads, and batch size for a model."""
```

Then change the tail of the function (`cli.py:1317-1320`):

```python
    console.print(f"[bold]{candidate.get('filename') or candidate.get('name')}[/bold]")
    _print_runtime_profile(
        tuning.recommend_runtime_settings(scan_hardware(), candidate)
    )
```

to:

```python
    profile = tuning.recommend_runtime_settings(scan_hardware(), candidate)
    if _global_opts().json:
        console.print_json(
            data={
                "model": candidate.get("filename") or candidate.get("name"),
                "profile_name": profile.profile_name,
                "context_length": profile.context_length,
                "gpu_offload_label": profile.gpu_offload_label,
                "cpu_threads": profile.cpu_threads,
                "num_batch": profile.num_batch,
                "available_memory_gb": profile.available_memory_gb,
                "headroom_gb": profile.headroom_gb,
            }
        )
        return
    console.print(f"[bold]{candidate.get('filename') or candidate.get('name')}[/bold]")
    _print_runtime_profile(profile)
```

- [ ] **Step 4: Write the tests**

In `tests/test_cli_memory_and_tune.py`, find the existing `tune` test(s) and add a `--json` case asserting the output starts with `{` and contains `"profile_name"`. In `tests/test_cli_setting.py`, add one case that invokes any one setting subcommand (e.g. `catalog-status`) with `--quiet` before and after the subcommand name and asserts exit code 0 (proving the decorator didn't break existing setting commands). In `tests/test_cli_update.py`, if it already mocks the pipx/git update flow, add a `--quiet` invocation and assert it doesn't crash (don't assert on progress-bar rendering specifics — `CliRunner` output capture and TTY-less runs already suppress Rich animation).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_memory_and_tune.py tests/test_cli_setting.py tests/test_cli_update.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_memory_and_tune.py tests/test_cli_setting.py tests/test_cli_update.py
git commit -m "feat: apply global flags to remaining commands, add tune --json"
```

---

### Task 5: Command aliases (`rm`, `ls`, `up`)

**Files:**
- Modify: `src/omm/cli.py` (`_RootHelpGroup` class, `cli.py:138-149`)
- Test: `tests/test_cli_help_version.py` (extend) or new `tests/test_cli_aliases.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks.

- [ ] **Step 1: Add alias resolution to `_RootHelpGroup`**

Change (`cli.py:138-149`):

```python
class _RootHelpGroup(typer.core.TyperGroup):
    """Homebrew-style curated `omm --help`/`omm help` - a short list of
    common commands instead of the full alphabetical listing of every
    registered subcommand. Full list stays reachable via `omm help --all`."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write(_ROOT_HELP_TEXT)
```

to:

```python
_COMMAND_ALIASES = {"rm": "uninstall", "ls": "list", "up": "upgrade"}


class _RootHelpGroup(typer.core.TyperGroup):
    """Homebrew-style curated `omm --help`/`omm help` - a short list of
    common commands instead of the full alphabetical listing of every
    registered subcommand. Full list stays reachable via `omm help --all`.
    Also resolves a handful of conventional short aliases (rm/ls/up) to
    their real command name - aliases are never registered as their own
    Click commands, so they never appear in a `commands` listing."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write(_ROOT_HELP_TEXT)

    def get_command(self, ctx: click.Context, cmd_name: str):
        cmd_name = _COMMAND_ALIASES.get(cmd_name, cmd_name)
        return super().get_command(ctx, cmd_name)
```

Note: `_COMMAND_ALIASES` must be defined before `_RootHelpGroup` uses it (both module-level, order as shown - `_ROOT_HELP_TEXT` is defined further down at `cli.py:113`, so this new dict can sit directly above the class, immediately below `PlainHelpFormatter`'s patching block and above where `_ROOT_HELP_TEXT` is currently defined; adjust placement so `_RootHelpGroup` still comes after `_ROOT_HELP_TEXT` since it references it in `format_help`).

- [ ] **Step 2: Mention the alias in each aliased command's help text**

Append one line to each docstring:
- `remove` (`cli.py:2103-2104`): `"""Uninstall a model and clean up all symlinks/manifests. Pass \`all\` to\n    uninstall every model installed via omm.\n\n    Alias: rm"""`
- `list_models` docstring: add `\n\n    Alias: ls`
- `upgrade` docstring: add `\n\n    Alias: up`

- [ ] **Step 3: Write the tests**

Create `tests/test_cli_aliases.py`:

```python
from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def test_rm_alias_resolves_to_uninstall(isolated_omm_home):
    result = runner.invoke(cli.app, ["rm", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Uninstall a model" in result.stdout


def test_ls_alias_resolves_to_list(isolated_omm_home):
    result = runner.invoke(cli.app, ["ls", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Show models installed via omm" in result.stdout


def test_up_alias_resolves_to_upgrade(isolated_omm_home):
    result = runner.invoke(cli.app, ["up", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "Refresh" in result.stdout or "Refresh an installed model" in result.stdout


def test_aliases_do_not_appear_in_help_all(isolated_omm_home):
    result = runner.invoke(cli.app, ["help", "--all"])
    assert result.exit_code == 0, result.stdout
    assert " rm " not in result.stdout.lower().replace("\n", " ")
```

(Adjust the exact docstring-substring assertions above to match whatever `upgrade`'s actual current docstring text is — confirm by reading `cli.py:2293-2295` before finalizing this test.)

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_aliases.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_aliases.py
git commit -m "feat: add rm/ls/up command aliases for uninstall/list/upgrade"
```

---

### Task 6: Per-command additions (`search --limit/--provider`, `--dry-run`, `--engine`)

**Files:**
- Modify: `src/omm/cli.py` (`search` 2688+, `remove`/uninstall 2093+, `upgrade` 2283+, `list_models` 2329+, `link_models` 2813+)
- Test: `tests/test_cli_search.py`, `tests/test_cli_remove.py`, `tests/test_cli_upgrade.py`, `tests/test_cli_list.py`, `tests/test_cli_link.py`

**Interfaces:**
- Consumes: `global_flags`/`_global_opts()` from Task 1, the already-decorated commands from Tasks 2-4.

- [ ] **Step 1: `search --limit` / `--provider`**

Add two parameters to `search` (after `skip_unfit`, before the closing `) -> None:`):

```python
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Show at most this many results."
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Only show results from this source: curated, huggingface, or modelscope.",
    ),
```

Validate `provider` right after the docstring:

```python
    if provider is not None and provider not in ("curated", "huggingface", "modelscope"):
        err_console.print(
            f"[red]--provider must be one of: curated, huggingface, modelscope (got '{provider}').[/red]"
        )
        raise typer.Exit(2)
```

Note `search`'s existing candidates don't currently carry a `"curated"` provider tag distinctly from `None`/absent — check `search_mod.local_candidate_pool`'s returned dicts for whichever key marks curated-vs-remote (likely `c.get("provider")` is `None`/absent for curated entries and `"huggingface"`/`"modelscope"` for the other two pools, based on the dedup logic at `cli.py:2704-2714` that already partitions `local_matches` vs `hf_matches`/`ms_matches`). Filter right after `combined = search_mod.dedupe_by_base_repo(...)` (`cli.py:2716`):

```python
    if provider is not None:
        if provider == "curated":
            combined = [c for c in combined if not c.get("provider")]
        else:
            combined = [c for c in combined if c.get("provider") == provider]
```

For `--limit`, apply it as a cap on the number of *printed/JSON* rows, not on candidates considered for grouping (so family headers still make sense) — the simplest correct place is right where `refs`/`rows` get appended, inside the `for c in groups[family]:` loop (`cli.py:2734+`): add a check right after `if skip_unfit and not fits_hardware: continue` (`cli.py:2758-2759`):

```python
            if limit is not None and len(refs) >= limit:
                break
```

and also `break` the outer `for family in sorted(groups):` loop once the limit is hit — wrap the outer loop body check: after the inner `for c in groups[family]` loop ends (`cli.py:2780`, `if not json_output and header_printed: console.print()`), add:

```python
        if limit is not None and len(refs) >= limit:
            break
```

- [ ] **Step 2: `uninstall`/`upgrade --dry-run`**

For `remove` (uninstall), add a parameter:

```python
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be uninstalled without removing anything."
    ),
```

In the `all` branch (`cli.py:2105-2115`), right after computing `reg`, before the confirmation:

```python
        if dry_run:
            for name in reg:
                console.print(f"Would uninstall: {name}")
            raise typer.Exit(0)
```

And for the single-model path, right before `_remove_one(filename, entry)` (`cli.py:2130`):

```python
    if dry_run:
        console.print(f"Would uninstall: {filename}")
        raise typer.Exit(0)
    _remove_one(filename, entry)
```

For `upgrade`, add the same `dry_run` parameter. In the "all" branch (`cli.py:2298-2313`), right after `if not reg: ...`:

```python
        if dry_run:
            for filename in reg:
                console.print(f"Would check for updates: {filename}")
            raise typer.Exit(0)
```

`autoremove --dry-run` is **dropped from this task's scope** (verified, not deferred-pending-a-check): `linker.autoremove_engine` (`linker.py:2181-2200`) dispatches to `autoremove_ollama`/`autoremove_lmstudio`/`autoremove_jan`/`autoremove_custom_directory`, and `linker.autoremove_owned_link` (`linker.py:311-318`) — none of these accept a preview/dry-run mode; every one of them deletes immediately and returns a count. Faking a "would remove N" number without the real detection logic would risk misreporting what's actually broken. Adding real dry-run support means threading a `dry_run` parameter through all of these linker functions, which is a separate, larger change outside this plan's scope — leave `autoremove` with no new flags in this task.

- [ ] **Step 3: `list`/`link --engine`**

For `list_models`, add:

```python
    engine: str | None = typer.Option(
        None, "--engine", help="Only show models linked into this engine."
    ),
```

Validate against `linker.ENGINES` keys right after the docstring:

```python
    valid_engines = {spec.key for spec in linker.ENGINES}
    if engine is not None and engine not in valid_engines:
        err_console.print(
            f"[red]--engine must be one of: {', '.join(sorted(valid_engines))} (got '{engine}').[/red]"
        )
        raise typer.Exit(2)
```

Then filter `reg` right after loading it (`cli.py:2334`, before the `if not reg:` check):

```python
    if engine is not None:
        reg = {
            filename: entry
            for filename, entry in reg.items()
            if entry.get("linked", {}).get(engine)
        }
```

For `link_models`, add `engine: str | None = typer.Option(None, "--engine", help="Only re-verify/repair links for this engine.")`. Validate the same way. Then in the no-`directory` branch, error if `directory is not None and engine is not None` ("`--engine` only applies without a directory argument."), and restrict the `for spec in linker.ENGINES:` loop (`cli.py:2892`) to `[s for s in linker.ENGINES if s.key == engine] if engine else linker.ENGINES`.

- [ ] **Step 4: Write/extend the tests**

- `tests/test_cli_search.py`: add `--limit 2` (assert at most 2 results/rows), `--provider curated` (assert only curated-sourced rows), `--provider bogus` (assert exit code 2 and the error message).
- `tests/test_cli_remove.py`: add `uninstall all --dry-run` (assert "Would uninstall" lines, assert registry unchanged after).
- `tests/test_cli_upgrade.py`: add `upgrade --dry-run` (assert "Would check for updates" lines).
- `tests/test_cli_list.py`: add `list --engine ollama` (assert only ollama-linked rows), `list --engine bogus` (assert exit code 2).
- `tests/test_cli_link.py`: add `link --engine ollama` (assert only ollama gets touched — check via whatever mock the existing link tests use for `linker.link_engine`/`is_engine_installed`).
- `tests/test_cli_autoremove.py`: no changes needed — `autoremove` gets no new flags in this task (see Step 2).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_search.py tests/test_cli_remove.py tests/test_cli_upgrade.py tests/test_cli_list.py tests/test_cli_link.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_search.py tests/test_cli_remove.py tests/test_cli_upgrade.py tests/test_cli_list.py tests/test_cli_link.py
git commit -m "feat: add search --limit/--provider, uninstall/upgrade --dry-run, list/link --engine"
```

---

### Task 7: `help --all` reimplementation

**Files:**
- Modify: `src/omm/cli.py` (`help_cmd`, `cli.py:250-273`)
- Test: `tests/test_cli_help_version.py` (extend `test_help_all_lists_every_command`)

**Interfaces:**
- Consumes: nothing from earlier tasks (independent of the flags work, but shares the file).

- [ ] **Step 1: Write the recursive full-reference renderer**

Change the `all` branch of `help_cmd` (`cli.py:258-263`):

```python
    if command is None:
        if all:
            formatter = root_ctx.make_formatter()
            typer.core.TyperGroup.format_help(root_ctx.command, root_ctx, formatter)
            console.print(formatter.getvalue().rstrip("\n"))
            raise typer.Exit(0)
        console.print(root_ctx.get_help())
        raise typer.Exit(0)
```

to:

```python
    if command is None:
        if all:
            _print_full_command_reference(root_ctx)
            raise typer.Exit(0)
        console.print(root_ctx.get_help())
        raise typer.Exit(0)
```

Then add a new helper function right above `help_cmd` (before its `@app.command(name="help")` decorator, so it's defined before use — Python doesn't require this given both live at module scope and `help_cmd` isn't called until runtime, but keep the file's existing top-to-bottom readability convention):

```python
def _print_full_command_reference(root_ctx: click.Context, *, _prefix: str = "") -> None:
    """Print every command's own --help text, recursing one level into
    nested groups (currently just `setting`). Reuses each Command's real
    get_help() output instead of a separate summary format, so every flag
    a command actually accepts always shows up here too."""
    names = sorted(root_ctx.command.commands.keys())
    first = True
    for name in names:
        cmd_obj = root_ctx.command.commands[name]
        if cmd_obj.hidden:
            continue
        if not first:
            console.print()
            console.print("---")
            console.print()
        first = False
        if isinstance(cmd_obj, click.Group):
            console.print(f"[bold]{_prefix}{name}[/bold] (command group)")
            console.print()
            _print_full_command_reference_group(cmd_obj, root_ctx, f"{_prefix}{name} ")
            continue
        sub_ctx = cmd_obj.make_context(name, [], parent=root_ctx, resilient_parsing=True)
        console.print(cmd_obj.get_help(sub_ctx))


def _print_full_command_reference_group(
    group: click.Group, parent_ctx: click.Context, prefix: str
) -> None:
    group_ctx = group.make_context(prefix.strip(), [], parent=parent_ctx, resilient_parsing=True)
    names = sorted(group.commands.keys())
    for idx, name in enumerate(names):
        cmd_obj = group.commands[name]
        if cmd_obj.hidden:
            continue
        if idx:
            console.print()
        sub_ctx = cmd_obj.make_context(f"{prefix}{name}", [], parent=group_ctx, resilient_parsing=True)
        console.print(cmd_obj.get_help(sub_ctx))
```

Note: `root_ctx.command.commands` is the dict Click's `MultiCommand`/`Group` keeps of registered subcommands (both the alias-resolving `_RootHelpGroup.get_command()` from Task 5 and this dict-based enumeration coexist fine — `get_command()` is only consulted when *dispatching* a typed command name; enumeration here reads the raw `.commands` dict directly, which never contains the alias keys since they were never registered as real commands, satisfying the spec's "aliases don't clutter `help --all`" requirement without any extra filtering).

- [ ] **Step 2: Extend the test**

In `tests/test_cli_help_version.py`, extend `test_help_all_lists_every_command`:

```python
def test_help_all_lists_every_command():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "autoremove" in result.stdout


def test_help_all_expands_nested_setting_subcommands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    for name in ("telemetry", "upload", "version", "calibrate", "catalog-trust", "catalog-status", "catalog-rollback"):
        assert name in result.stdout, f"missing setting subcommand: {name}"


def test_help_all_shows_flags_not_just_command_names():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "--skip-unfit" in result.stdout
    assert "--json" in result.stdout


def test_help_all_excludes_hidden_commands():
    result = runner.invoke(cli.app, ["help", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "_bg-version-check" not in result.stdout
```

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_help_version.py -v`
Expected: all PASS.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_help_version.py
git commit -m "feat: help --all shows every command including nested setting subcommands and their flags"
```

---

### Task 8: README updates

**Files:**
- Modify: `README.md` (`## Usage` block ~87-118, `### Scripting` ~122-128)

**Interfaces:**
- Consumes: the final flag/alias set from Tasks 1-7 (do this task last).

- [ ] **Step 1: Update the `## Usage` code block**

In `README.md:89-118`, update these lines to reflect the new flags:

```
omm search <query> [--json] [--skip-unfit] [--limit N] [--provider curated|huggingface|modelscope]  # Search curated models, cached candidates, and HuggingFace
omm install <name> [--skip-unfit] [--upload/--no-upload] [--force]  # Download a model and link it into LM Studio / Ollama
omm uninstall <name> [--dry-run] # Uninstall a model and clean up its symlinks/manifests (alias: rm)
omm uninstall all [--yes] [--dry-run]  # Uninstall every model installed via omm
omm list [--json] [--engine NAME]    # Show models installed via omm and their linked status (alias: ls)
omm upgrade <name> [--dry-run]   # Refresh a model against its source if it has changed since install (alias: up)
omm upgrade [--yes] [--dry-run]  # Check every installed model for updates
omm link [--engine NAME]             # Re-verify and repair every installed model's LM Studio/Ollama links
omm tune <name> [--json]  # Recommend context, GPU offload, threads, and batch size
omm scan [--json]             # Print a hardware, runner, and model summary (RAM, VRAM, OS)
```

(Keep every other line in that block as-is; only the ones listed above change.)

- [ ] **Step 2: Document the global flags and exit codes in `### Scripting`**

In `README.md`, right after the existing first paragraph of `### Scripting` (line 124), insert a new paragraph:

```
Four global flags work either before or after the subcommand name (`omm --json search foo` and `omm search foo --json` are equivalent): `--json` (structured output, where supported — currently `search`/`list`/`info`/`benchmark`/`tune`/`scan`), `--yes`/`-y` (skip confirmation prompts, works on every command that has one), `--quiet`/`-q` (suppress banners and informational output), and `--no-color` (disable ANSI colors; the `NO_COLOR` environment variable does the same). Exit codes are consistent across every command: `0` success, `1` failure, `2` usage error (bad flag/argument).

`rm`, `ls`, and `up` are short aliases for `uninstall`, `list`, and `upgrade`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document global flags, aliases, and new per-command options"
```

---

## Final Verification

- [ ] Run the entire suite once more end to end: `.venv/bin/pytest -q`
- [ ] Manually smoke-test in a throwaway `OMM_HOME` (per the isolated-pipx-testing recipe used in past sessions — never against the real installed `omm`): `omm --json scan`, `omm scan --json`, `omm help --all | less`, `omm rm --help`, `omm ls`, `omm up --help`.
