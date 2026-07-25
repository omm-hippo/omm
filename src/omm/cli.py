"""omm CLI entry point (apt/brew-style command routing)."""

from __future__ import annotations

import json
import math
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click
import typer
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from omm import (
    benchmark,
    benchmark_history,
    calibration,
    catalog,
    config as config_mod,
    linker,
    predictor,
    quality as quality_mod,
    registry,
    rules as rules_mod,
    scan_import,
    search as search_mod,
    session_cache,
    telemetry,
    trust,
    tuning,
    version_check,
)
from omm import contribute as contribute_mod
from omm.completion import complete_install_name, complete_remove_filename
from omm.config import MODELS_DIR, OMM_HOME, load_config, save_config
from omm.downloader import DownloadCancelled, DownloadError, download_file
from omm.hardware import HardwareInfo, calculate_memory_budget, scan_hardware
from omm.hashutil import sha256_file
from omm.featurize import (
    candidate_active_parameter_count_billions,
    candidate_parameter_count_billions,
    candidate_quant_bits,
    parse_param_count_billions,
    parse_quant_bits,
)
from omm.hub import (
    AmbiguousModelError,
    AmbiguousProviderError,
    ModelResolutionError,
    QuantVariant,
    ResolvedModel,
    best_filenames_by_tier,
    download_url,
    fetch_repo_param_count_b,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    resolve_model,
)

class PlainHelpFormatter(click.HelpFormatter):
    """Homebrew-style help formatter: no panels/borders, uppercase section headers."""

    def write_usage(self, prog: str, args: str = "", prefix: str | None = None) -> None:
        super().write_usage(prog, args, prefix="USAGE: ")

    def write_heading(self, heading: str) -> None:
        self.write(f"{'':>{self.current_indent}}{heading.upper()}:\n")


click.Context.formatter_class = PlainHelpFormatter
try:
    # Typer >=0.16 vendors its own click fork (typer._click) instead of
    # using the `click` package's Context directly, so patching
    # click.Context alone leaves Typer's own help rendering unaffected.
    from typer._click.core import Context as _TyperClickContext

    _TyperClickContext.formatter_class = PlainHelpFormatter
except ImportError:
    pass

app = typer.Typer(
    name="omm",
    help="Open source Model Manager - package manager for local LLMs (GGUF).",
    rich_markup_mode=None,
)
setting_app = typer.Typer(
    name="setting",
    help="View or change omm settings (UI mode, telemetry, upload policy, catalog trust).",
    invoke_without_command=True,
    rich_markup_mode=None,
)
app.add_typer(setting_app)
console = Console()
err_console = Console(stderr=True)

REPO_URL = "git+https://github.com/omm-hippo/omm.git"


def _load_recommendation_with_change_note(config: dict) -> tuple[dict | None, bool]:
    manifest_url = config.get("catalog_manifest_url")
    public_key = config.get("catalog_public_key")
    if manifest_url and public_key:
        return predictor.load_model_with_change_note(
            config.get("model_url"), manifest_url, public_key
        )
    return predictor.load_model_with_change_note(config.get("model_url"))


def _omm_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("omm")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _telemetry_destination_line() -> str:
    """Human-readable summary of where install/contribute telemetry goes,
    shown under the bare `omm` version banner."""
    config = load_config()
    policy = config.get("telemetry_send_policy", "ask")
    endpoint = config.get("telemetry_endpoint")
    backend = config.get("telemetry_backend", "local")

    if policy == "never" or not endpoint:
        return "Data: not sent anywhere (telemetry disabled)"

    if backend == "firebase_legacy":
        return f"Data: sent to Firebase - {endpoint}"
    if backend == "self_hosted":
        return f"Data: sent to self-hosted server (FastAPI+SQLite) - {endpoint}"
    return f"Data: sent to {endpoint}"


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        commit = _installed_commit()
        suffix = f" ({commit[:7]})" if commit else ""
        console.print(f"omm {_omm_version()}{suffix}")
        console.print(f"[dim]{_telemetry_destination_line()}[/dim]")
        raise typer.Exit(0)
    _maybe_auto_import(ctx)
    resent = telemetry.flush_pending()
    if resent:
        console.print(
            f"[dim]Sent {resent} queued telemetry event(s) from a previous session.[/dim]"
        )


@app.command(name="help")
def help_cmd(
    ctx: typer.Context,
    command: str = typer.Argument(None, help="Show help for a specific subcommand."),
) -> None:
    """Show help, same as --help."""
    root_ctx = ctx.find_root()
    if command is None:
        console.print(root_ctx.get_help())
        raise typer.Exit(0)

    cmd_obj = root_ctx.command.get_command(root_ctx, command)
    if cmd_obj is None:
        err_console.print(f"[red]No such command '{command}'. See `omm help`.[/red]")
        raise typer.Exit(1)

    sub_ctx = cmd_obj.make_context(command, [], parent=root_ctx, resilient_parsing=True)
    console.print(cmd_obj.get_help(sub_ctx))


def _install_spec() -> str:
    """NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since
    2016) - only pull that extra in on other platforms, mirroring
    install.sh. Points at the persistent local clone (SRC_DIR) rather than
    the git URL directly, since omm installs it --editable."""
    if platform.system() == "Darwin":
        return str(SRC_DIR)
    return f"{SRC_DIR}[nvidia]"


def _shorten_home(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _link_repair_needed(reg: dict) -> bool:
    """True if some omm-hub model isn't yet symlinked into an installed
    engine (e.g. Ollama/LM Studio was installed after the model was)."""
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    for filename, entry in reg.items():
        if not (MODELS_DIR / filename).exists():
            continue
        linked = entry.get("linked", {})
        for key, is_installed in installed.items():
            if is_installed and not linked.get(key):
                return True
    return False


def _reconcile_stale_link_records(reg: dict, installed: dict[str, bool]) -> list[str]:
    """Clear `linked[engine]=True` registry records for engines that are no
    longer installed. Uninstalling an engine takes its data dir - and any
    omm symlinks inside it - with it, so there's nothing left to unlink;
    this just stops the registry from claiming a dead link still exists.
    Returns the filenames whose registry entry was corrected."""
    cleaned = []
    for filename, entry in reg.items():
        linked = entry.get("linked", {})
        stale = {key: False for key, on in linked.items() if on and not installed.get(key, False)}
        if not stale:
            continue
        linked.update(stale)
        registry.upsert_entry(filename, linked=stale)
        cleaned.append(filename)
    return cleaned


@app.command()
def scan() -> None:
    """Scan current PC hardware (RAM, VRAM, OS) and print a summary table."""
    info = scan_hardware()

    table = Table(title="omm hardware scan")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    table.add_row("RAM (available)", f"{info.ram_available_gb:.1f} GB")
    budget = calculate_memory_budget(info)
    table.add_row("Safe model budget now", f"{budget.model_budget_gb:.1f} GB")
    table.add_row("Reserved for apps/OS", f"{budget.ram_safety_reserve_gb:.1f} GB+")

    if info.unified_memory:
        table.add_row("Memory type", "Unified (Apple Silicon)")
        table.add_row("GPU", info.gpu_name or "Unknown")
    elif info.gpu_name:
        table.add_row("GPU", info.gpu_name)
        table.add_row("VRAM (total)", f"{info.vram_total_gb:.1f} GB")
        table.add_row("VRAM (free)", f"{info.vram_free_gb:.1f} GB")
    else:
        table.add_row("GPU", "None detected (no NVIDIA GPU found)")

    console.print(table)

    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}

    engine_table = Table(title="Local AI runners", box=None)
    engine_table.add_column("Program", style="cyan")
    engine_table.add_column("Status", style="white")
    for spec in linker.ENGINES:
        engine_table.add_row(spec.label, "installed" if installed[spec.key] else "not detected")
    console.print()
    console.print(engine_table)

    reg = registry.load_registry()
    cleaned = _reconcile_stale_link_records(reg, installed)
    external = scan_import.find_external_models()

    model_table = Table(title="Local AI models", box=None)
    model_table.add_column("Model", style="cyan")
    model_table.add_column("Location", style="white")
    model_table.add_column("Engine(s)")
    model_table.add_column("Managed by omm")
    for filename, entry in reg.items():
        linked = entry.get("linked", {})
        engines = [name for name, on in linked.items() if on]
        model_table.add_row(filename, "(omm hub)", ", ".join(engines) or "-", "yes")
    for item in external:
        model_table.add_row(item.display_name, _shorten_home(item.path), item.engine, "no")
    console.print()
    console.print(model_table)

    if cleaned:
        console.print()
        console.print(
            f"Cleared stale link record(s) for: {', '.join(cleaned)} "
            "(engine no longer installed)."
        )
    if _link_repair_needed(reg):
        console.print()
        console.print(
            "Some omm-hub models aren't linked into an installed engine yet. "
            "Run: omm link"
        )
    if external:
        console.print()
        console.print(
            "Found model file(s) outside the omm hub. Run: omm import"
        )


def _refresh_data() -> None:
    """Unconditionally re-fetch rules.json and recommend-model.json from
    their configured URLs (used by `omm update` for a full data sync)."""
    import requests

    config = load_config()

    rules_url = config.get("rules_url")
    if rules_url:
        try:
            fetched = rules_mod.fetch_rules(rules_url)
            console.print(f"[green]Updated rules.json ({len(fetched)} entries) from {rules_url}[/green]")
        except requests.RequestException as e:
            err_console.print(f"[red]Failed to fetch rules from {rules_url}: {e}[/red]")
    else:
        console.print("[dim]No rules_url configured - using bundled defaults.[/dim]")

    model_url = config.get("model_url")
    if model_url:
        try:
            manifest_url = config.get("catalog_manifest_url")
            public_key = config.get("catalog_public_key")
            if manifest_url and public_key:
                artifact = predictor.fetch_and_cache_model(model_url, manifest_url, public_key)
            else:
                artifact = predictor.fetch_and_cache_model(model_url)
            console.print(
                f"[green]Updated recommend-model.json "
                f"({len(artifact.get('candidates', []))} candidates) from {model_url}[/green]"
            )
        except (requests.RequestException, ValueError) as e:
            err_console.print(f"[red]Failed to fetch trained model from {model_url}: {e}[/red]")


_BARE_REPO_URL = REPO_URL.removeprefix("git+")

SRC_DIR = OMM_HOME / "src"


def _src_head_commit() -> str | None:
    """HEAD commit of the persistent editable clone at SRC_DIR, if this
    install has migrated to the git-pull update mechanism. None if not
    migrated yet, or if the clone is missing/corrupted (triggers
    self-healing re-migration in update())."""
    if not (SRC_DIR / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(SRC_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _installed_commit() -> str | None:
    """The commit omm is actually running from. Checks the persistent
    editable clone (SRC_DIR) first, then falls back to pip's PEP 610
    direct_url.json vcs_info - present for not-yet-migrated installs that
    still used a plain `pipx install <git-URL>` VCS snapshot."""
    import importlib.metadata

    src_commit = _src_head_commit()
    if src_commit:
        return src_commit
    try:
        raw = importlib.metadata.distribution("omm").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    return json.loads(raw).get("vcs_info", {}).get("commit_id")


def _remote_head_commit(ref: str = "main") -> str | None:
    """Latest commit on the given ref of the omm repo, via `git ls-remote`
    (no GitHub API rate limit, no auth needed for a public repo)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", _BARE_REPO_URL, ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _cached_remote_head_commit(ref: str = "main") -> str | None:
    return version_check.cached_remote_head(_remote_head_commit, ref)


_SKIP_UPDATE_CHECK_SUBCOMMANDS = {"update", "help", "_bg-version-check"}


@app.command(name="_bg-version-check", hidden=True)
def _bg_version_check_cmd() -> None:
    """Internal. Spawned by `_maybe_start_update_check` as a detached child
    so the `git ls-remote` round trip survives the short-lived parent
    command exiting; writes the result to the shared cache for a later
    `omm` invocation to pick up."""
    version_check.cached_remote_head(_remote_head_commit)


def _confirm_and_print_update_notice(cached_latest: str, installed: str) -> None:
    """The cached remote head can be up to _TTL_SECONDS stale, so a mismatch
    against it is only a hint, not proof. Before alarming the user, re-check
    live and refresh the cache - this trades a bit of extra latency (only on
    the rare command where the notice would otherwise fire) for never showing
    a stale "update available" once the real remote has caught up."""
    latest = _remote_head_commit()
    if latest is None:  # offline/unreachable - don't guess, stay silent
        return
    version_check.record(latest)
    if latest != installed:
        err_console.print("[yellow]Update available! Run: [bold]omm update[/bold][/yellow]")


def _maybe_start_update_check(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand in _SKIP_UPDATE_CHECK_SUBCOMMANDS:
        return
    installed = _installed_commit()
    if not installed:  # editable/dev install - nothing to compare against
        return
    fresh, latest = version_check.cached_remote_head_if_fresh()
    if fresh:
        if latest and latest != installed:
            ctx.call_on_close(lambda: _confirm_and_print_update_notice(latest, installed))
        return
    if version_check.should_start_check():
        version_check.mark_checking()
        try:
            subprocess.Popen(
                [sys.executable, "-m", "omm.cli", "_bg-version-check"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


_SKIP_AUTO_IMPORT_SUBCOMMANDS = {"update", "help", "import", "_bg-version-check"}


def _maybe_auto_import(ctx: typer.Context) -> None:
    """One-time, best-effort offer to adopt stray .gguf files already
    sitting in Ollama's/LM Studio's own directories into the omm hub.
    Runs on the first interactive command after install (not from
    install.sh itself - curl|sh has no TTY for questionary's prompts) and
    never again once the flag is set, whether or not anything was found."""
    if ctx.invoked_subcommand in _SKIP_AUTO_IMPORT_SUBCOMMANDS:
        return
    config = load_config()
    if config.get("external_scan_done"):
        return
    if not sys.stdin.isatty():
        return
    config["external_scan_done"] = True
    save_config(config)
    _run_import_flow()


def _run_import_flow(extra_path: Path | None = None, *, yes: bool = False) -> None:
    import questionary

    found = scan_import.find_external_models(extra_path)
    groups = scan_import.group_by_hash(found)
    if not groups:
        console.print("[dim]No externally-managed .gguf files found.[/dim]")
        return

    total_gb = sum(g.size_bytes for g in groups) / (1024**3)
    console.print(
        f"Found {len(groups)} model(s) ({len(found)} file(s), ~{total_gb:.1f} GB) "
        "in supported local AI apps not yet managed by omm."
    )
    if not yes and not _ask_confirm(f"Import {len(groups)} model(s) into the omm hub?"):
        err_console.print("[yellow]Skipped.[/yellow]")
        return

    if yes:
        selected_hashes = [g.sha256 for g in groups]
    else:
        choices = [
            questionary.Choice(
                title=f"{g.display_name} ({g.size_bytes / (1024**3):.1f} GB, found in: {', '.join(g.engines)})",
                value=g.sha256,
                checked=True,
            )
            for g in groups
        ]
        selected_hashes = _ask_select(questionary.checkbox("Select which models to import:", choices=choices))
    if not selected_hashes:
        err_console.print("[yellow]Nothing selected, skipped.[/yellow]")
        return

    bytes_saved = 0
    for group in groups:
        if group.sha256 not in selected_hashes:
            continue
        result = scan_import.adopt_group(group)
        bytes_saved += result.bytes_saved
        console.print(f"  [green]Imported {result.filename}[/green]")

    final_count = len(registry.load_registry())
    console.print(
        f"[bold green]Done: {final_count} model(s) in the omm hub, "
        f"{bytes_saved / (1024**3):.1f} GB saved.[/bold green]"
    )


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


# pipx gives no byte-level install progress, but it does print a fixed,
# ordered sequence of stage lines to stdout - use those as real (if coarse)
# progress checkpoints instead of an indeterminate animation that never
# actually reflects how far along the install is.
_PIPX_INSTALL_STAGES = [
    "creating virtual environment",
    "determining package name",
    "installing omm from spec",
    "done!",
    "installed package",
]


def _run_pipx_install(args: list[str], progress: Progress, task_id) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines: list[str] = []
    stage = 0
    for line in proc.stdout:
        output_lines.append(line)
        lowered = line.lower()
        for i in range(stage, len(_PIPX_INSTALL_STAGES)):
            if _PIPX_INSTALL_STAGES[i] in lowered:
                stage = i + 1
                progress.update(task_id, completed=stage)
                break
    returncode = proc.wait()
    output = "".join(output_lines)
    return subprocess.CompletedProcess(args, returncode, stdout=output, stderr=output)


def _declared_dependency_names() -> list[str] | None:
    """Package names from the freshly-pulled SRC_DIR/pyproject.toml's
    [project] dependencies, or None if the file can't be read/parsed."""
    try:
        text = (SRC_DIR / "pyproject.toml").read_text()
    except OSError:
        return None
    match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not match:
        return None
    names = []
    for spec in re.findall(r'"([^"]+)"', match.group(1)):
        name = re.split(r"[<>=!~;\s\[]", spec, maxsplit=1)[0]
        if name:
            names.append(name)
    return names


def _deps_satisfied() -> bool:
    """True if every dependency declared in the freshly-pulled
    pyproject.toml is importable in this venv (no network, <0.05s).

    Checks against the live pyproject.toml rather than `pip check`:
    an editable install's dist-info is frozen at the last full `pipx
    install`, so `pip check` (which only validates consistency between
    already-installed packages) can't see a dependency that was newly
    added to source since then - it always reports satisfied, so
    `omm update` would silently skip installing it."""
    import importlib.metadata

    names = _declared_dependency_names()
    if names is None:
        return False
    for name in names:
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return False
    return True


def _run_pipx_install_with_progress(args: list[str]) -> subprocess.CompletedProcess:
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]Reinstalling omm via pipx...[/cyan]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("upgrade", total=len(_PIPX_INSTALL_STAGES))
        result = _run_pipx_install(args, progress, task_id)
        progress.update(task_id, completed=len(_PIPX_INSTALL_STAGES))
    return result


def _migrate_to_editable_install() -> subprocess.CompletedProcess:
    """First-run (or self-heal) path: clone the repo into a scratch dir,
    swap it into place as SRC_DIR only once the clone has actually
    succeeded, then pipx --editable-install it, so future `omm update`
    calls are a `git pull` instead of a full pipx reinstall. Runs whenever
    SRC_DIR isn't a valid git checkout - regardless of whether the
    currently installed commit already matches latest, since the goal is
    switching mechanism, not code.

    Clones into SRC_DIR.new rather than SRC_DIR directly: a clone that
    fails partway (network drop, timeout, Ctrl-C) must not destroy a
    working editable install - previously an rmtree-then-clone order left
    `omm` permanently broken with ModuleNotFoundError until reinstalled
    from scratch."""
    console.print("[cyan]Migrating to fast-update mode (one-time)...[/cyan]")
    tmp_dir = SRC_DIR.with_name(SRC_DIR.name + ".new")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        clone = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--quiet", _BARE_REPO_URL, str(tmp_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return subprocess.CompletedProcess([], 1, stdout="", stderr="git clone timed out")
    if clone.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return clone

    head = subprocess.run(
        ["git", "-C", str(tmp_dir), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
    )
    if head.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return head
    # Verified against the *currently running* omm's own bundled anchor
    # (the old, already-vetted install) - not tmp_dir's own copy, which an
    # attacker with push access could have edited in the same commit.
    ok, message = trust.verify_commit(tmp_dir, head.stdout.strip(), trust.current_trust_anchor())
    if not ok:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return subprocess.CompletedProcess([], 1, stdout="", stderr=message)

    shutil.rmtree(SRC_DIR, ignore_errors=True)
    tmp_dir.rename(SRC_DIR)
    return _run_pipx_install_with_progress(
        ["pipx", "install", "--force", "--editable", _install_spec()]
    )


def _run_git(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="git command timed out")


def _git_update_src() -> subprocess.CompletedProcess:
    """Fast path for an already-migrated install: fetch + fast-forward the
    persistent clone in place. The editable install's .pth points straight
    at SRC_DIR/src, so this alone is enough to pick up code changes - no
    pipx call needed unless dependencies themselves changed (checked by
    the caller via _deps_satisfied()).

    Verifies origin/main's signature against the anchor still on disk from
    *before* this fetch (SRC_DIR's own trust/allowed_signers, read prior to
    reset --hard overwriting it) before ever checking the new commit out."""
    fetch = _run_git(["git", "-C", str(SRC_DIR), "fetch", "--quiet", "origin", "main"])
    if fetch.returncode != 0:
        return fetch

    rev_parse = _run_git(["git", "-C", str(SRC_DIR), "rev-parse", "origin/main"], timeout=10)
    if rev_parse.returncode != 0:
        return rev_parse
    target_commit = rev_parse.stdout.strip()

    ok, message = trust.verify_commit(SRC_DIR, target_commit, trust.current_trust_anchor())
    if not ok:
        return subprocess.CompletedProcess([], 1, stdout="", stderr=message)

    return _run_git(["git", "-C", str(SRC_DIR), "reset", "--hard", "--quiet", "origin/main"])


@app.command()
def update() -> None:
    """Reinstall omm from the latest source, then refresh rules/model data.
    Uses a persistent editable clone (SRC_DIR) for a git-pull-speed update
    once migrated; a one-time pipx --editable install otherwise."""
    migrated = _src_head_commit() is not None
    installed = _installed_commit()
    latest = _remote_head_commit() if installed else None
    if latest:
        version_check.record(latest)
    if migrated and installed and latest and installed == latest:
        console.print(f"[green]omm is already up to date ({installed[:7]}).[/green]")
        _refresh_data()
        return

    try:
        if not migrated:
            result = _migrate_to_editable_install()
        else:
            console.print(f"Updating omm from {REPO_URL} ...")
            result = _git_update_src()
            if result.returncode == 0 and not _deps_satisfied():
                result = _run_pipx_install_with_progress(
                    ["pipx", "install", "--force", "--editable", _install_spec()]
                )
    except FileNotFoundError:
        err_console.print(
            "[red]git or pipx not found. Install them first, or rerun the installer:[/red]\n"
            "  curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh"
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        err_console.print(f"[red]Update failed:[/red]\n{result.stderr}")
        raise typer.Exit(1)

    console.print("[green]omm reinstalled from the latest source.[/green]")
    _refresh_data()


def _add_escape_to_cancel(question: questionary.Question) -> questionary.Question:
    """questionary only aborts on Ctrl+C/Ctrl+Q by default; make Escape do
    the same so `.ask()` returns None instead of requiring Ctrl+C."""

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    question.application.key_bindings.add(Keys.Escape, eager=True)(_abort)
    return question


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _require_tty(what: str) -> None:
    """Interactive prompts (questionary) have no non-interactive fallback
    and will hang or misbehave without a real terminal. Fail loudly and
    immediately instead, so scripts/CI get a clear error rather than a
    hang. Callers that have a flag to bypass the prompt entirely should
    check that flag before ever reaching this."""
    if not sys.stdin.isatty():
        err_console.print(
            f"[red]{what} requires an interactive terminal. "
            "Re-run from a real terminal, or pass the flag that bypasses this prompt.[/red]"
        )
        raise typer.Exit(1)


def _ask_select(question: questionary.Question):
    _require_tty("This selection")
    return _add_escape_to_cancel(question).ask()


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/no prompt that answers on the y/n keypress itself (no Enter
    needed) via questionary's auto_enter. questionary.confirm's internal
    key bindings are already merged by the time we get the Question object,
    so (unlike _ask_select) we can't bolt an Escape binding on here -
    Ctrl+C/Ctrl+Q still cancel via questionary's own bindings."""
    import questionary

    _require_tty(message)
    answer = questionary.confirm(message, default=default, auto_enter=True).ask()
    return bool(answer)


def _resolve_upload_decision(prompt: str) -> bool:
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "always":
        return True
    if policy == "never":
        return False
    return _ask_confirm(prompt)


@app.command()
def recommend() -> None:
    """Scan hardware and suggest a model to install, ranked by a model
    trained on real install telemetry (falls back to static rules if the
    trained model can't be fetched)."""
    import questionary
    import requests

    info = scan_hardware()
    config = load_config()

    artifact, changed = _load_recommendation_with_change_note(config)
    if changed:
        console.print("[dim]Fetched updated recommendation data from GitHub.[/dim]")
    if artifact and artifact.get("candidates"):
        ranked = predictor.rank_candidates(artifact, info)
        viable = [(c, speed) for c, speed in ranked if speed > 0][:10]
        if not viable:
            err_console.print("[red]No model is predicted to run on this hardware.[/red]")
            raise typer.Exit(1)

        refs = [search_mod.install_ref(c) for c, speed in viable]
        session_cache.record_seen(refs)
        choices = [
            questionary.Choice(
                title=f"{c['name']} (~{speed:.0f} tok/s predicted) - {c.get('description', '')}",
                value=ref,
            )
            for (c, speed), ref in zip(viable, refs)
        ]
        selected = _ask_select(questionary.select("Pick a model to install:", choices=choices))
        if selected is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(selected)
        return

    console.print("[dim]No trained model available, falling back to static rules.[/dim]")
    rules_url = config.get("rules_url")
    if rules_url:
        try:
            _, rules_changed = rules_mod.refresh_rules_with_change_note(rules_url)
            if rules_changed:
                console.print("[dim]Fetched updated rules from GitHub.[/dim]")
        except requests.RequestException:
            pass

    has_gpu = info.vram_total_gb is not None
    available_gb = calculate_memory_budget(info).install_budget_gb

    rule_list = rules_mod.load_rules()
    matches = rules_mod.matching_rules(rule_list, available_gb, has_gpu=has_gpu)

    if not matches:
        err_console.print("[red]No model in the current rules fits this hardware.[/red]")
        raise typer.Exit(1)

    session_cache.record_seen([r["name"] for r in matches])
    choices = [
        questionary.Choice(title=f"{r['name']} - {r['description']}", value=r["name"])
        for r in matches
    ]
    selected = _ask_select(questionary.select("Pick a model to install:", choices=choices))
    if selected is None:
        err_console.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(0)

    install(selected)


def _print_runtime_profile(profile: tuning.RuntimeProfile) -> None:
    table = Table(title=f"Recommended {profile.profile_name} runtime profile")
    table.add_column("Setting", style="cyan")
    table.add_column("Starting value")
    table.add_row("Context length", f"{profile.context_length:,} tokens")
    table.add_row("GPU offload", profile.gpu_offload_label)
    table.add_row("CPU threads", str(profile.cpu_threads))
    table.add_row("Batch size", str(profile.num_batch))
    table.add_row("Safe model budget now", f"{profile.available_memory_gb:.1f} GB")
    if profile.headroom_gb is not None:
        table.add_row("Estimated memory headroom", f"{profile.headroom_gb:.1f} GB")
    console.print(table)
    console.print(
        "[dim]These are conservative starting values; benchmark before "
        "treating them as optimal.[/dim]"
    )


@app.command()
def tune(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
) -> None:
    """Recommend context, GPU offload, threads, and batch size for a model."""
    model_name = _resolve_ref(model_name)
    filename, entry = _lookup_entry(model_name, registry.load_registry())

    if entry is not None:
        candidate = {
            "name": filename,
            "filename": filename,
            "repo_id": entry.get("repo_id"),
            "size_bytes": entry.get("size_bytes"),
        }
    else:
        try:
            resolved = resolve_model(model_name)
        except (AmbiguousModelError, ModelResolutionError) as error:
            err_console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from error
        candidate = {
            "name": resolved.filename,
            "filename": resolved.filename,
            "repo_id": resolved.repo_id,
        }
        artifact = predictor.load_cached_model()
        if artifact:
            candidate = next(
                (
                    published
                    for published in artifact.get("candidates", [])
                    if published.get("repo_id") == resolved.repo_id
                    and published.get("filename") == resolved.filename
                ),
                candidate,
            )

    console.print(f"[bold]{candidate.get('filename') or candidate.get('name')}[/bold]")
    _print_runtime_profile(
        tuning.recommend_runtime_settings(scan_hardware(), candidate)
    )


def _resolve_ref(arg: str) -> str:
    """If `arg` is a bare integer, treat it as a 1-based index into the last
    `omm search`/`omm list` results shown in this terminal. Any non-numeric
    arg passes through unchanged."""
    if not arg.isdigit():
        return arg

    results = session_cache.load_last_results()
    if not results:
        err_console.print(
            "[red]Run `omm search` or `omm list` first to install/uninstall by number.[/red]"
        )
        raise typer.Exit(1)

    idx = int(arg)
    if idx < 1 or idx > len(results):
        err_console.print(f"[red]No result #{idx} (1-{len(results)}).[/red]")
        raise typer.Exit(1)

    return results[idx - 1]


def _resolve_benchmark_tag(arg: str) -> str:
    """Like `_resolve_ref`, but a numbered ref names a filename from the last
    `omm search`/`omm list`, which `omm benchmark` needs as an Ollama tag."""
    if not arg.isdigit():
        return arg
    filename = _resolve_ref(arg)
    entry = registry.load_registry().get(filename)
    tag = entry.get("ollama_name") if entry else None
    if not tag:
        err_console.print(f"[red]{filename} has no Ollama tag; link it with `omm link` first.[/red]")
        raise typer.Exit(1)
    return tag


def _predicted_fastest_filenames(
    variants: list[QuantVariant],
    repo_id: str | None,
    hw: HardwareInfo,
    parameter_count_b: float | None = None,
) -> set[str]:
    """Filenames that are the fastest-predicted variant in their quant-bits
    tier, per the cached ML speed model. Empty when no model is cached, so
    callers fall back to plain (uncolored) rendering."""
    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    if trees is None:
        return set()

    predicted_speed = {}
    for variant in variants:
        if variant.fits is not True:
            continue
        candidate = {
            "repo_id": repo_id,
            "filename": variant.filename,
            "parameter_count_b": parameter_count_b,
        }
        speed = predictor.predict_speed(trees, hw, candidate)
        if speed > 0:
            predicted_speed[variant.filename] = speed

    return best_filenames_by_tier(variants, predicted_speed)


def _pick_quant_variant(error: AmbiguousModelError) -> str | None:
    """Rank the ambiguous repo's .gguf files by fit against this PC's RAM/VRAM
    and let the user pick one, cursor defaulted to the best-fitting, highest
    quality option. The predicted-fastest variant in each quant-bits tier is
    highlighted in green, per the cached ML speed model (skipped entirely if
    no model is cached)."""
    import questionary

    info = scan_hardware()
    available_gb = calculate_memory_budget(info).install_budget_gb

    variants = rank_quant_variants(error.candidates, available_gb, error.param_count_b)
    resolved_variants = []
    for variant in variants:
        if variant.required_gb is not None:
            resolved_variants.append(variant)
            continue
        size_bytes = remote_file_size(error.provider, error.repo_id, variant.filename)
        if size_bytes is None:
            resolved_variants.append(variant)
            continue
        required_gb = size_bytes / (1024**3) * 1.2
        resolved_variants.append(
            type(variant)(
                filename=variant.filename,
                quant_bits=variant.quant_bits,
                required_gb=required_gb,
                fits=required_gb <= available_gb,
            )
        )
    variants = sorted(
        resolved_variants,
        key=lambda variant: (variant.fits is not True, -(variant.quant_bits or 0)),
    )
    fastest_filenames = _predicted_fastest_filenames(
        variants, error.repo_id, info, error.param_count_b
    )

    choices = []
    for v in variants:
        if v.fits is True:
            note = f"✓ fits, ~{v.required_gb:.1f}GB needed"
        elif v.fits is False:
            note = f"may not fit, ~{v.required_gb:.1f}GB needed"
        else:
            note = "fit unknown"
        if v.filename in fastest_filenames:
            title = [("fg:green bold", f"{v.filename}  ({note}, predicted fastest)")]
        else:
            title = f"{v.filename}  ({note})"
        choices.append(questionary.Choice(title=title, value=v.filename))

    return _ask_select(
        questionary.select(f"Select a quantization variant for '{error.repo_id}':", choices=choices)
    )


def _link_model(dest, repo_id: str | None, ollama_tag: str) -> dict[str, bool]:
    """Link a downloaded .gguf into every installed engine, printing a skip
    notice for whichever engine isn't installed or fails to link. Shared
    by `install` and `update` since both need the exact same behavior
    after a fresh (or refreshed) download."""
    linked = {spec.key: False for spec in linker.ENGINES}

    for spec in linker.ENGINES:
        if not linker.is_engine_installed(spec.key):
            console.print(f"[dim]{spec.label} not detected, skipping link.[/dim]")
            continue
        try:
            warning = linker.link_engine(spec.key, dest, repo_id=repo_id, ollama_tag=ollama_tag)
            linked[spec.key] = True
            if warning:
                err_console.print(f"[yellow]{warning}[/yellow]")
        except linker.LinkError as e:
            err_console.print(f"[yellow]{spec.label} link skipped: {e}[/yellow]")

    return linked


@dataclass
class InstallOutcome:
    filename: str
    repo_id: str | None
    linked: dict[str, bool]
    ollama_tag: str | None = None
    tokens_per_sec: float | None = None
    telemetry_sent: bool = False
    skipped_unfit: bool = False
    sha256: str | None = None


class ContributionStopped(Exception):
    """Esc fired mid-download or mid-benchmark inside `_install_impl`
    while running under `omm contribute`."""

    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self.filename = filename


class _Interrupted(Exception):
    pass


def _run_interruptible(fn, stop_event: threading.Event | None):
    """Run `fn()`, but if `stop_event` fires while it's in flight, return
    control (raising `_Interrupted`) instead of blocking until `fn`
    finishes. With no `stop_event`, just calls `fn()` directly - no thread
    pool overhead on the plain `omm install` path."""
    if stop_event is None:
        return fn()

    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeoutError

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        while True:
            if stop_event.is_set():
                raise _Interrupted()
            try:
                return future.result(timeout=0.2)
            except _FuturesTimeoutError:
                continue
    finally:
        pool.shutdown(wait=False)


def _maybe_auto_calibrate(
    filename: str, repo_id: str | None, dest: Path, tokens_per_sec: float
) -> None:
    """Best-effort local calibration right after a successful benchmark.
    Silent no-op if there's no cached model to compare against - this must
    never block or fail the install."""
    artifact = predictor.load_cached_model()
    if not artifact or not artifact.get("trees"):
        return
    hardware = scan_hardware()
    candidate = {
        "repo_id": repo_id,
        "filename": filename,
        "size_bytes": dest.stat().st_size if dest.exists() else None,
    }
    try:
        predicted, _, _ = predictor.predict_speed_interval(
            artifact["trees"],
            hardware,
            candidate,
            engine="ollama",
            apply_calibration=False,
        )
    except (ValueError, KeyError, TypeError, IndexError):
        return
    if predicted <= 0:
        return
    factor = calibration.record_calibration(
        hardware,
        measured_tokens_per_sec=tokens_per_sec,
        predicted_tokens_per_sec=predicted,
        engine="ollama",
    )
    console.print(
        f"[dim]Local calibration updated: correction ×{factor:.2f} "
        "(not uploaded).[/dim]"
    )


def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    no_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
    use_quality_eval: bool = False,
    quality_pack: dict | None = None,
) -> InstallOutcome:
    """Core of `omm install`: download, link, register, benchmark+calibrate
    automatically, optionally report telemetry. Shared by the plain
    `install` command and `omm contribute`'s unattended loop via the
    kwargs above."""
    url, filename, repo_id = resolved.url, resolved.filename, resolved.repo_id

    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    if trees is not None:
        hw = scan_hardware()
        candidate = {"repo_id": repo_id, "filename": filename}
        speed = predictor.predict_speed(trees, hw, candidate)
        if speed <= 0:
            err_console.print(
                f"[red]Warning: this hardware is predicted not to run {filename}.[/red]"
            )
            if skip_unfit:
                return InstallOutcome(filename, repo_id, linked={}, skipped_unfit=True)
            if not _ask_confirm("Install anyway?"):
                err_console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)
        else:
            try:
                _, speed_low, speed_high = predictor.predict_speed_interval(trees, hw, candidate)
            except (ValueError, KeyError, TypeError, IndexError):
                speed_low = speed_high = speed
            console.print(
                f"[dim]Predicted speed: {speed:.1f} tok/s "
                f"(range {speed_low:.1f}–{speed_high:.1f}).[/dim]"
            )

    dest = MODELS_DIR / filename
    if dest.exists():
        err_console.print(f"[yellow]{filename} already downloaded, skipping fetch.[/yellow]")
    else:
        try:
            if stop_event is not None:
                download_file(url, dest, stop_check=stop_event.is_set)
            else:
                download_file(url, dest)
        except DownloadCancelled as e:
            raise ContributionStopped(filename) from e
        except DownloadError as e:
            err_console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

    console.print("Verifying checksum...")
    sha256 = sha256_file(dest)

    ollama_tag = linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)

    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        source=url,
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        repo_id=repo_id,
        provider=resolved.provider or "huggingface",
        linked=linked,
    )

    tokens_per_sec = None
    telemetry_sent = False
    sample_count = 1
    speed_min = speed_max = None
    quality_summary = None
    runtime = None
    model_metadata = None
    engine_version = None
    runtime_options = None
    if linked["ollama"]:
        console.print("Benchmarking...")
        runtime_hw = scan_hardware()
        runtime_candidate = {
            "filename": filename, "repo_id": repo_id, "size_bytes": dest.stat().st_size,
        }
        try:
            model_metadata = quality_mod._model_metadata(ollama_tag)
            runtime_candidate.update(model_metadata)
        except quality_mod.QualityEvaluationError:
            model_metadata = None
        runtime_options = tuning.recommend_runtime_settings(runtime_hw, runtime_candidate).ollama_options
        if use_quality_eval:
            try:
                def _evaluate_with_runtime():
                    try:
                        return quality_mod.evaluate_model(
                            ollama_tag, quality_pack, speed_runs=3, runtime_options=runtime_options
                        )
                    except TypeError:  # compatibility with older integrations
                        return quality_mod.evaluate_model(ollama_tag, quality_pack, speed_runs=3)
                result = _run_interruptible(
                    _evaluate_with_runtime,
                    stop_event,
                )
            except _Interrupted as e:
                raise ContributionStopped(filename) from e
            except quality_mod.QualityEvaluationError:
                result = None
            finally:
                quality_mod.unload_model(ollama_tag)
            if result is not None:
                tokens_per_sec = result["speed"]["median_tokens_per_sec"]
                samples = result["speed"]["samples_tokens_per_sec"]
                sample_count = result["speed"]["runs"]
                speed_min, speed_max = min(samples), max(samples)
                quality_summary = {
                    "pack_id": quality_pack["pack_id"],
                    "pack_version": quality_pack.get("pack_version"),
                    "correct": result["quality"]["correct"],
                    "total": result["quality"]["total"],
                    "accuracy": result["quality"]["accuracy"],
                }
                runtime = result.get("runtime")
                model_metadata = result
                engine_version = quality_mod.ollama_version()
        else:
            try:
                sampled = _run_interruptible(
                    lambda: benchmark.benchmark_ollama_samples(
                        ollama_tag, runs=3, options=runtime_options
                    ), stop_event
                )
                if sampled is not None:
                    tokens_per_sec = sampled["median_tokens_per_sec"]
                    sample_count = sampled["count"]
                    speed_min, speed_max = sampled["min_tokens_per_sec"], sampled["max_tokens_per_sec"]
                    runtime = quality_mod.runtime_snapshot(
                        ollama_tag, (model_metadata or {}).get("digest"), runtime_options
                    )
                    engine_version = quality_mod.ollama_version()
            except _Interrupted as e:
                raise ContributionStopped(filename) from e
            finally:
                quality_mod.unload_model(ollama_tag)

        if tokens_per_sec:
            console.print(f"[cyan]{tokens_per_sec:.1f} tok/s[/cyan]")
            _maybe_auto_calibrate(filename, repo_id, dest, tokens_per_sec)

            want_upload = not no_upload and (
                auto_upload or _resolve_upload_decision(
                    "Send this machine's benchmark result to the server?"
                )
            )
            if want_upload:
                telemetry_sent = _report_telemetry(
                    filename,
                    repo_id,
                    tokens_per_sec,
                    sample_count=sample_count,
                    speed_min=speed_min,
                    speed_max=speed_max,
                    quality=quality_summary,
                    model_metadata=model_metadata,
                    runtime=runtime,
                    engine_version=engine_version,
                    model_filename=filename,
                    model_digest=sha256,
                    provider=resolved.provider,
                )
            else:
                telemetry.log_attempt("declined_by_user", filename)
        else:
            telemetry_sent = _report_telemetry(
                filename, repo_id, tokens_per_sec, provider=resolved.provider
            )
    else:
        telemetry.log_attempt("not_attempted_no_ollama_link", filename)

    return InstallOutcome(
        filename, repo_id, linked, ollama_tag, tokens_per_sec, telemetry_sent, sha256=sha256
    )


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
    try:
        resolved = resolve_model(model_name)
    except AmbiguousModelError as e:
        chosen = _pick_quant_variant(e)
        if chosen is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{e.provider}:{e.repo_id}:{chosen}", skip_unfit=skip_unfit, upload=upload)
        return
    except AmbiguousProviderError as e:
        choices = [
            questionary.Choice(title=provider, value=provider) for provider in e.providers
        ]
        chosen_provider = _ask_select(
            questionary.select(f"'{e.repo_id}' found on multiple providers, pick one:", choices=choices)
        )
        if chosen_provider is None:
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{chosen_provider}:{e.repo_id}", skip_unfit=skip_unfit, upload=upload)
        return
    except ModelResolutionError as e:
        err_console.print(f"[red]{e}[/red]")
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e

    outcome = _install_impl(
        resolved,
        skip_unfit=skip_unfit,
        auto_upload=upload is True,
        no_upload=upload is False,
    )

    console.print(f"[green]Installed {outcome.filename}[/green]")
    if outcome.linked.get("ollama"):
        console.print(f"  Ollama: [green]ollama run {outcome.ollama_tag}[/green]")
    for spec in linker.ENGINES:
        if spec.key != "ollama" and outcome.linked.get(spec.key):
            console.print(f"  {spec.label}: visible in your local models list")
    console.print(f"  Uninstall with: [cyan]omm uninstall {outcome.filename}[/cyan]")


def _cleanup_incomplete_install(filename: str) -> bool:
    dest = MODELS_DIR / filename
    part = dest.with_suffix(dest.suffix + ".part")
    cleaned = False
    if part.exists():
        part.unlink()
        cleaned = True
    if dest.exists():
        dest.unlink()
        cleaned = True
    return cleaned


def _remove_one(filename: str, entry: dict) -> None:
    linked = entry.get("linked", {})
    for spec in linker.ENGINES:
        if linked.get(spec.key):
            linker.unlink_engine(spec.key, filename, entry)

    dest = MODELS_DIR / filename
    dest.unlink(missing_ok=True)
    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)

    registry.remove_entry(filename)
    console.print(f"[green]Removed {filename}[/green]")


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
    """Uninstall a model and clean up all symlinks/manifests. Pass `all` to
    uninstall every model installed via omm."""
    if filename.lower() == "all":
        reg = registry.load_registry()
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if not yes and not _ask_confirm(f"Uninstall all {len(reg)} model(s)?"):
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        for name, entry in list(reg.items()):
            _remove_one(name, entry)
        return

    filename = _resolve_ref(filename)
    reg = registry.load_registry()
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        if _cleanup_incomplete_install(filename):
            console.print(f"[green]Cleaned up incomplete install of {filename}[/green]")
            raise typer.Exit(0)
        err_console.print(f"[red]{filename} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    _remove_one(filename, entry)


def _lookup_entry(filename: str, reg: dict) -> tuple[str, dict] | tuple[None, None]:
    """Find a registry entry by exact filename, retrying with a `.gguf`
    suffix appended (mirrors the lookup `remove` already does)."""
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        return None, None
    return filename, entry


def _entry_version(entry: dict) -> str:
    return entry.get("version") or (entry.get("sha256") or "")[:7] or "unknown"


@app.command()
def info(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
    json_output: bool = typer.Option(False, "--json", help="Print result as JSON instead of a table."),
) -> None:
    """Show name, version, size, and linked-program run commands for an installed model."""
    model_name = _resolve_ref(model_name)
    reg = registry.load_registry()
    filename, entry = _lookup_entry(model_name, reg)
    if entry is None:
        err_console.print(f"[red]{model_name} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    size_gb = entry.get("size_bytes", 0) / (1024**3)
    linked = entry.get("linked", {})

    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)

    if json_output:
        console.print_json(
            data={
                "filename": filename,
                "repo_id": entry.get("repo_id"),
                "provider": entry.get("provider") or ("huggingface" if entry.get("repo_id") else None),
                "version": _entry_version(entry),
                "size_bytes": entry.get("size_bytes", 0),
                "installed_at": entry.get("installed_at", "unknown"),
                "linked": {spec.key: bool(linked.get(spec.key)) for spec in linker.ENGINES},
                "ollama_run_command": f"ollama run {ollama_tag}" if linked.get("ollama") else None,
            }
        )
        return

    table = Table(title=filename, show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    repo_label = entry.get("repo_id") or "(direct URL install)"
    provider = entry.get("provider")
    if entry.get("repo_id") and provider and provider != "huggingface":
        repo_label = f"{repo_label} [{provider}]"
    table.add_row("Repo", repo_label)
    table.add_row("Version", _entry_version(entry))
    table.add_row("Size", f"{size_gb:.2f} GB")
    table.add_row("Installed at", entry.get("installed_at", "unknown"))
    for spec in linker.ENGINES:
        if spec.key == "ollama":
            table.add_row("Ollama", f"ollama run {ollama_tag}" if linked.get("ollama") else "not linked")
        else:
            table.add_row(
                spec.label,
                f"linked (visible in {spec.label})" if linked.get(spec.key) else "not linked",
            )

    console.print(table)


def _update_one(filename: str, entry: dict) -> str:
    """Refresh one installed model against its source. Returns "updated",
    "up_to_date", or "skipped". HF-repo installs check a cheap remote hash
    first and only re-download on a mismatch; direct-URL installs have no
    such endpoint, so they re-download to a temp file and compare hashes
    before swapping it in."""
    dest = MODELS_DIR / filename
    repo_id = entry.get("repo_id")
    provider = entry.get("provider") or "huggingface"
    old_sha256 = entry.get("sha256")

    if repo_id:
        remote_sha256 = remote_file_sha256(provider, repo_id, filename)
        if remote_sha256 is None:
            err_console.print(
                f"[yellow]{filename}: could not check for updates "
                "(no repo/LFS info), skipped.[/yellow]"
            )
            return "skipped"
        if remote_sha256 == old_sha256:
            return "up_to_date"

        url = download_url(provider, repo_id, filename)
        try:
            download_file(url, dest)
        except DownloadError as e:
            err_console.print(f"[red]{filename}: update download failed: {e}[/red]")
            return "skipped"
        new_sha256 = sha256_file(dest)
    else:
        source = entry.get("source")
        if not source:
            err_console.print(f"[yellow]{filename}: no source URL on record, skipped.[/yellow]")
            return "skipped"

        tmp = dest.with_name(dest.name + ".update")
        try:
            download_file(source, tmp)
        except DownloadError as e:
            err_console.print(f"[red]{filename}: update download failed: {e}[/red]")
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(tmp.suffix + ".part").unlink(missing_ok=True)
            return "skipped"

        new_sha256 = sha256_file(tmp)
        if new_sha256 == old_sha256:
            tmp.unlink(missing_ok=True)
            return "up_to_date"
        tmp.replace(dest)

    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)
    registry.upsert_entry(
        filename,
        sha256=new_sha256,
        version=new_sha256[:7],
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        provider=provider,
        linked=linked,
    )
    return "updated"


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
    """Refresh an installed model against its source, re-downloading only
    if the source has changed since install. With no argument (or `all`),
    checks every model installed via omm."""
    reg = registry.load_registry()

    if model_name is None or model_name.lower() == "all":
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if not yes and not _ask_confirm(f"Check {len(reg)} model(s) for updates?"):
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

        counts = {"updated": 0, "up_to_date": 0, "skipped": 0}
        for filename, entry in list(reg.items()):
            counts[_update_one(filename, entry)] += 1
        console.print(
            f"[green]{counts['updated']} updated, {counts['up_to_date']} up to date, "
            f"{counts['skipped']} skipped.[/green]"
        )
        return

    resolved = _resolve_ref(model_name)
    filename, entry = _lookup_entry(resolved, reg)
    if entry is None:
        err_console.print(f"[red]{resolved} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    result = _update_one(filename, entry)
    if result == "up_to_date":
        console.print(f"[green]{filename} is already up to date ({_entry_version(entry)}).[/green]")
    elif result == "updated":
        fresh_entry = registry.load_registry()[filename]
        console.print(f"[green]{filename} updated to {_entry_version(fresh_entry)}.[/green]")


@app.command(name="list")
def list_models(
    json_output: bool = typer.Option(False, "--json", help="Print results as JSON instead of a table."),
) -> None:
    """Show models installed via omm and their linked status."""
    reg = registry.load_registry()
    if not reg:
        if json_output:
            console.print_json(data=[])
        else:
            console.print("No models installed via omm yet. Try `omm recommend` or `omm install`.")
        raise typer.Exit(0)

    if json_output:
        rows = [
            {
                "index": idx,
                "filename": filename,
                "size_bytes": entry.get("size_bytes", 0),
                "linked": {spec.key: bool(entry.get("linked", {}).get(spec.key)) for spec in linker.ENGINES},
            }
            for idx, (filename, entry) in enumerate(reg.items(), start=1)
        ]
        console.print_json(data=rows)
        session_cache.record_results(list(reg.keys()))
        return

    table = Table(title="omm models")
    table.add_column("#", justify="right")
    table.add_column("Filename", style="cyan")
    table.add_column("Size", justify="right")
    detailed = load_config().get("ui_mode") == "detailed"
    # Detailed mode only adds a column per engine actually installed here -
    # with 7 possible engines, always showing all of them would make the
    # table unreadably wide for the common case of 1-2 installed apps.
    detailed_specs = [spec for spec in linker.ENGINES if linker.is_engine_installed(spec.key)] if detailed else []
    if detailed:
        for spec in detailed_specs:
            table.add_column(spec.label)
    else:
        table.add_column("Links")

    for idx, (filename, entry) in enumerate(reg.items(), start=1):
        size_gb = entry.get("size_bytes", 0) / (1024**3)
        linked = entry.get("linked", {})
        if detailed:
            table.add_row(
                str(idx),
                filename,
                f"{size_gb:.2f} GB",
                *("[green]yes[/green]" if linked.get(spec.key) else "no" for spec in detailed_specs),
            )
        else:
            programs = [spec.label for spec in linker.ENGINES if linked.get(spec.key)]
            table.add_row(
                str(idx), filename, f"{size_gb:.2f} GB", ", ".join(programs) or "none"
            )
    console.print(table)
    session_cache.record_results(list(reg.keys()))


@setting_app.command(name="ui")
def configure_ui(
    mode: str = typer.Argument(None, help="compact or detailed"),
) -> None:
    """Choose compact everyday tables or detailed per-engine columns."""
    current = load_config()
    if mode is not None:
        normalized = mode.lower()
        if normalized not in {"compact", "detailed"}:
            err_console.print("[red]UI mode must be compact or detailed.[/red]")
            raise typer.Exit(1)
        current = config_mod.update_config(ui_mode=normalized)
    console.print(f"UI mode: [cyan]{current.get('ui_mode', 'compact')}[/cyan]")


@setting_app.command(name="telemetry")
def configure_telemetry(
    endpoint: str = typer.Option(
        None,
        "--endpoint",
        help="Self-hosted HTTPS endpoint, localhost URL, or 'none' to clear it.",
    ),
) -> None:
    """Configure where benchmark telemetry is sent; see `omm setting upload` for the send policy."""
    current = load_config()
    changes = {}
    if endpoint is not None:
        if endpoint.lower() == "none":
            changes.update(telemetry_endpoint=None, telemetry_backend="local")
        elif not telemetry.secure_endpoint(endpoint):
            err_console.print("[red]Use HTTPS, or HTTP only for localhost.[/red]")
            raise typer.Exit(1)
        else:
            changes.update(
                telemetry_endpoint=endpoint,
                telemetry_backend=(
                    "firebase_legacy" if "firebaseio.com" in endpoint else "self_hosted"
                ),
            )
    if changes:
        current = config_mod.update_config(**changes)
    table = Table(title="Telemetry destination", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Backend", str(current.get("telemetry_backend") or "local"))
    table.add_row("Endpoint", str(current.get("telemetry_endpoint") or "not configured"))
    console.print(table)


@setting_app.command(name="upload")
def configure_upload(
    enable: bool = typer.Option(False, "--enable", help="Always send benchmark results without asking."),
    disable: bool = typer.Option(False, "--disable", help="Never send benchmark results."),
    ask: bool = typer.Option(False, "--ask", help="Ask every time before sending (default)."),
) -> None:
    """Configure the benchmark-upload send policy; see `omm setting telemetry` for the destination."""
    chosen = [flag for flag in (enable, disable, ask) if flag]
    if len(chosen) > 1:
        err_console.print("[red]Choose only one of --enable, --disable, or --ask.[/red]")
        raise typer.Exit(1)
    current = load_config()
    changes = {}
    if enable:
        if not current.get("telemetry_endpoint"):
            err_console.print("[red]Set an endpoint with `omm setting telemetry --endpoint` before enabling uploads.[/red]")
            raise typer.Exit(1)
        changes["telemetry_send_policy"] = "always"
    elif disable:
        changes["telemetry_send_policy"] = "never"
    elif ask:
        changes["telemetry_send_policy"] = "ask"
    if changes:
        current = config_mod.update_config(**changes)
    table = Table(title="Benchmark upload policy", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    policy = current.get("telemetry_send_policy", "ask")
    table.add_row("Uploads", {"always": "always", "never": "never", "ask": "ask (default)"}[policy])
    console.print(table)


@setting_app.command(name="calibrate")
def calibrate(
    model_name: str = typer.Argument(
        None,
        help="Installed Ollama-linked model; defaults to the smallest available model.",
    ),
) -> None:
    """Correct this machine's local speed estimate without uploading data."""
    reg = registry.load_registry()
    eligible = [
        (filename, entry)
        for filename, entry in reg.items()
        if (entry.get("linked") or {}).get("ollama")
    ]
    if not eligible:
        err_console.print("[red]No Ollama-linked omm models are installed.[/red]")
        raise typer.Exit(1)
    if model_name is None:
        filename, entry = min(eligible, key=lambda item: item[1].get("size_bytes") or 2**63)
    else:
        resolved = _resolve_ref(model_name)
        filename, entry = _lookup_entry(resolved, reg)
        if entry is None or not (entry.get("linked") or {}).get("ollama"):
            err_console.print(f"[red]{resolved} is not linked to Ollama.[/red]")
            raise typer.Exit(1)

    artifact = predictor.load_cached_model()
    if not artifact or not artifact.get("trees"):
        err_console.print("[red]No cached recommendation model is available.[/red]")
        raise typer.Exit(1)
    hardware = scan_hardware()
    candidate = {
        "repo_id": entry.get("repo_id"),
        "filename": filename,
        "size_bytes": entry.get("size_bytes"),
    }
    predicted, _, _ = predictor.predict_speed_interval(
        artifact["trees"],
        hardware,
        candidate,
        engine="ollama",
        apply_calibration=False,
    )
    if predicted <= 0:
        err_console.print("[red]This model has no usable baseline speed prediction.[/red]")
        raise typer.Exit(1)
    tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    measured = benchmark.benchmark_ollama(tag)
    if measured is None or measured <= 0:
        err_console.print("[red]Calibration requires a running Ollama model server.[/red]")
        raise typer.Exit(1)
    factor = calibration.record_calibration(
        hardware,
        measured_tokens_per_sec=measured,
        predicted_tokens_per_sec=predicted,
        engine="ollama",
    )
    console.print(
        f"[green]Local calibration saved: {measured:.1f} tok/s measured, "
        f"{predicted:.1f} predicted, correction ×{factor:.2f}.[/green]"
    )
    console.print("[dim]The calibration stays in ~/.omm and was not uploaded.[/dim]")


@setting_app.command(name="catalog-trust")
def catalog_trust(
    manifest_url: str = typer.Option(..., "--manifest-url", help="HTTPS manifest URL."),
    public_key: str = typer.Option(..., "--public-key", help="Base64 Ed25519 public key."),
) -> None:
    """Require future recommendation downloads to pass signature verification."""
    if not manifest_url.startswith("https://"):
        err_console.print("[red]The signed catalog manifest must use HTTPS.[/red]")
        raise typer.Exit(1)
    try:
        fingerprint = catalog.public_key_fingerprint(public_key)
    except catalog.CatalogVerificationError as error:
        err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error
    config_mod.update_config(
        catalog_manifest_url=manifest_url,
        catalog_public_key=public_key,
    )
    console.print(f"[green]Signed catalog verification enabled (key {fingerprint}).[/green]")


@setting_app.command(name="catalog-status")
def catalog_status() -> None:
    """Show recommendation-catalog trust and rollback state."""
    current = load_config()
    public_key = current.get("catalog_public_key")
    fingerprint = "not configured"
    if isinstance(public_key, str):
        try:
            fingerprint = catalog.public_key_fingerprint(public_key)
        except catalog.CatalogVerificationError:
            fingerprint = "invalid"
    table = Table(title="Recommendation catalog", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Signed manifest", str(current.get("catalog_manifest_url") or "not configured"))
    table.add_row("Trusted key", fingerprint)
    table.add_row("Rollback snapshots", str(len(catalog.snapshots())))
    console.print(table)


@setting_app.command(name="catalog-rollback")
def catalog_rollback() -> None:
    """Restore the most recent different recommendation snapshot."""
    try:
        selected = catalog.rollback()
    except (OSError, ValueError) as error:
        err_console.print(f"[red]Catalog rollback failed: {error}[/red]")
        raise typer.Exit(1) from error
    console.print(f"[green]Rolled back recommendation catalog from {selected.name}.[/green]")


@setting_app.callback(invoke_without_command=True)
def setting_menu(ctx: typer.Context) -> None:
    """Bare `omm setting` opens an interactive menu; a subcommand skips it."""
    import questionary

    if ctx.invoked_subcommand is not None:
        return
    while True:
        choice = _ask_select(
            questionary.select(
                "What do you want to change?",
                choices=[
                    questionary.Choice("UI mode", value="ui"),
                    questionary.Choice("Telemetry", value="telemetry"),
                    questionary.Choice("Upload", value="upload"),
                    questionary.Choice("Calibrate", value="calibrate"),
                    questionary.Choice("Catalog trust", value="catalog-trust"),
                    questionary.Choice("Catalog status", value="catalog-status"),
                    questionary.Choice("Catalog rollback", value="catalog-rollback"),
                ],
            )
        )
        if choice is None:
            return

        if choice == "ui":
            mode = _ask_select(
                questionary.select("UI mode:", choices=["compact", "detailed"])
            )
            if mode is not None:
                configure_ui(mode)
        elif choice == "telemetry":
            endpoint = questionary.text(
                "Endpoint (blank to keep current, 'none' to clear):"
            ).ask()
            configure_telemetry(endpoint=endpoint or None)
        elif choice == "upload":
            action = _ask_select(
                questionary.select(
                    "Uploads:",
                    choices=[
                        questionary.Choice("Always send", value="enable"),
                        questionary.Choice("Never send", value="disable"),
                        questionary.Choice("Ask every time", value="ask"),
                        questionary.Choice("Leave unchanged", value="skip"),
                    ],
                )
            )
            if action is not None:
                configure_upload(
                    enable=(action == "enable"),
                    disable=(action == "disable"),
                    ask=(action == "ask"),
                )
        elif choice == "calibrate":
            model_name = questionary.text(
                "Model to calibrate (blank for smallest installed):"
            ).ask()
            calibrate(model_name or None)
        elif choice == "catalog-trust":
            manifest_url = questionary.text("Signed manifest URL (https://...):").ask()
            public_key = questionary.text("Base64 Ed25519 public key:").ask()
            if manifest_url and public_key:
                catalog_trust(manifest_url=manifest_url, public_key=public_key)
        elif choice == "catalog-status":
            catalog_status()
        elif choice == "catalog-rollback":
            if _ask_confirm("Roll back the recommendation catalog?"):
                catalog_rollback()


@app.command()
def search(
    query: str,
    json_output: bool = typer.Option(False, "--json", help="Print results as JSON instead of a table."),
    skip_unfit: bool = typer.Option(
        False,
        "--skip-unfit",
        help="If this hardware is predicted not to run a model, omit it "
        "from the results instead of listing it.",
    ),
) -> None:
    """Search curated models, cached candidates, and HuggingFace by name."""
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    local_matches = search_mod.match_candidates(pool, query)

    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    hf_matches = [
        c
        for c in search_mod.search_huggingface(query)
        if c.get("repo_id") not in local_repo_ids
    ]
    ms_matches = [
        c
        for c in search_mod.search_modelscope(query)
        if c.get("repo_id") not in local_repo_ids
    ]

    combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches + ms_matches)
    if not combined:
        err_console.print(f"[yellow]No models found matching '{query}'.[/yellow]")
        raise typer.Exit(1)

    # Score against whatever's already cached locally, same as install
    # completion - the only network calls are the lazy per-repo param-count
    # fallback below, for repo names too unusual to parse.
    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    hw = scan_hardware() if trees else None

    groups = search_mod.group_by_family(combined)
    refs: list[str] = []
    seen_refs: set[str] = set()
    rows: list[dict] = []
    for family in sorted(groups):
        header_printed = False
        for c in groups[family]:
            ref = search_mod.install_ref(c)
            if ref in seen_refs:
                continue
            desc = c.get("description") or ""
            candidate = c
            if (
                trees is not None
                and c.get("repo_id")
                and candidate_parameter_count_billions(c) is None
            ):
                # Filename/repo-id parsing found no param count (e.g. a repo
                # branded "DeepSeek-V4-Flash" instead of "...-70B"). Without
                # this, estimate_required_memory_gb can't tell "fits" from
                # "unknown", and the "predicted not to run" warning silently
                # never fires for exactly the huge models that most need it.
                param_count_b = fetch_repo_param_count_b(
                    c.get("provider") or "huggingface", c["repo_id"]
                )
                if param_count_b is not None:
                    candidate = {**c, "parameter_count_b": param_count_b}
            fits_hardware = not (
                trees is not None and predictor.predict_speed(trees, hw, candidate) <= 0
            )
            if skip_unfit and not fits_hardware:
                continue
            seen_refs.add(ref)
            refs.append(ref)
            if json_output:
                rows.append(
                    {
                        "index": len(refs),
                        "family": family,
                        "ref": ref,
                        "description": desc,
                        "fits_hardware": fits_hardware,
                    }
                )
            else:
                if not header_printed:
                    console.print(f"[bold cyan]==> {family}[/bold cyan]")
                    header_printed = True
                if fits_hardware:
                    console.print(f"  [{len(refs)}] {ref}  [dim]{desc}[/dim]")
                else:
                    console.print(f"  [{len(refs)}] [red]{ref}  (predicted not to run on this hardware)[/red]")
        if not json_output and header_printed:
            console.print()

    session_cache.record_results(refs)
    if json_output:
        console.print_json(data=rows)


def _print_install_suggestions(query: str) -> None:
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    suggestions = search_mod.dedupe_by_base_repo(search_mod.suggest_similar(query, pool, limit=3))

    existing_labels = {s.get("name") or s.get("repo_id") for s in suggestions}
    if len(suggestions) < 3:
        for hit in search_mod.search_huggingface(query, limit=5):
            if len(suggestions) >= 3:
                break
            label = hit.get("name") or hit.get("repo_id")
            if label in existing_labels:
                continue
            suggestions.append(hit)
            existing_labels.add(label)

    if not suggestions:
        return

    err_console.print("[yellow]Did you mean one of these?[/yellow]")
    for s in suggestions:
        err_console.print(f"  - {search_mod.install_ref(s)}")


@app.command(name="link")
def link_models(
    directory: Path = typer.Argument(
        None,
        help="Optional model directory for an unsupported local AI app.",
    ),
) -> None:
    """Link models into an arbitrary directory or repair known app links.

    Without a directory, re-verify every installed model's links into every
    supported app (Ollama, LM Studio, Jan, AnythingLLM, Msty,
    text-generation-webui, KoboldCpp) and repair them. Covers models that
    were never linked *and* ones whose link is now broken, missing, or
    stale - link_engine() always replaces the existing symlink/manifest, so
    this always re-links rather than trusting the registry's stored
    `linked` flag. With a directory, reuse the central GGUF through
    non-copying symlinks for another local application."""
    reg = registry.load_registry()
    if not reg:
        console.print("No models installed via omm yet.")
        raise typer.Exit(0)

    if directory is not None:
        directory = directory.expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        linked_count = 0
        skipped_missing = 0
        for filename, entry in reg.items():
            source = MODELS_DIR / filename
            if not source.exists():
                skipped_missing += 1
                continue
            try:
                destination = linker.link_custom_directory(source, directory)
            except linker.LinkError as error:
                err_console.print(f"[yellow]{filename}: custom link skipped: {error}[/yellow]")
                continue
            custom_links = list(entry.get("custom_links") or [])
            if str(destination) not in custom_links:
                custom_links.append(str(destination))
            registry.upsert_entry(filename, custom_links=custom_links)
            linked_count += 1
        console.print(
            f"[green]{linked_count} model(s) linked into {directory}.[/green] "
            f"{skipped_missing} skipped (file missing)."
        )
        return

    relinked_count = 0
    skipped_missing = 0

    for filename, entry in reg.items():
        dest = MODELS_DIR / filename
        if not dest.exists():
            skipped_missing += 1
            continue

        ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
        new_linked: dict[str, bool] = {}
        changed = False

        for spec in linker.ENGINES:
            if not linker.is_engine_installed(spec.key):
                continue
            try:
                linker.link_engine(spec.key, dest, repo_id=entry.get("repo_id"), ollama_tag=ollama_tag)
                new_linked[spec.key] = True
                changed = True
            except linker.LinkError as e:
                err_console.print(f"[yellow]{filename}: {spec.label} link skipped: {e}[/yellow]")

        if changed:
            registry.upsert_entry(filename, linked=new_linked, ollama_name=ollama_tag)
            relinked_count += 1

    console.print(
        f"[green]{relinked_count} model(s) relinked/verified.[/green] "
        f"{skipped_missing} skipped (file missing)."
    )


@app.command(name="relink", hidden=True)
def relink() -> None:
    """Deprecated alias for `omm link`."""
    err_console.print("[yellow]`omm relink` is deprecated; use `omm link`.[/yellow]")
    link_models(directory=None)


def _autoremove_incomplete_installs() -> int:
    if not MODELS_DIR.exists():
        return 0

    reg = registry.load_registry()
    removed = 0
    for path in MODELS_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix == ".part":
            if path.with_suffix("").name not in reg:
                path.unlink()
                removed += 1
        elif path.suffix == ".gguf" and path.name not in reg:
            path.unlink()
            removed += 1
    return removed


@app.command()
def autoremove() -> None:
    """Remove broken symlinks left behind when a model's source .gguf was
    deleted without going through `omm uninstall`, plus any orphaned partial or
    unregistered downloads in the models directory."""
    removed_by_engine: dict[str, int] = {}
    for spec in linker.ENGINES:
        if linker.is_engine_installed(spec.key):
            removed_by_engine[spec.label] = linker.autoremove_engine(spec.key)
    incomplete_removed = _autoremove_incomplete_installs()

    if not any(removed_by_engine.values()) and incomplete_removed == 0:
        console.print("[green]No broken symlinks found.[/green]")
        return

    parts = [f"{count} broken {label} link(s)" for label, count in removed_by_engine.items() if count]
    console.print(
        f"[green]Removed {', '.join(parts) or '0 broken links'}, "
        f"{incomplete_removed} incomplete install file(s) cleaned up.[/green]"
    )


@app.command(name="benchmark")
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
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Also print the evidence JSON.",
    ),
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
    models = [_resolve_benchmark_tag(m) for m in models]
    started_daemon = None
    if not benchmark.ollama_daemon_reachable():
        if _stdin_is_tty() and _ask_confirm(
            "Ollama isn't running. Start it now, benchmark, then stop it afterward?"
        ):
            started_daemon = benchmark.start_ollama_daemon()
            if started_daemon is None:
                err_console.print("[red]Couldn't start the Ollama daemon.[/red]")
                raise typer.Exit(1)
        else:
            err_console.print("[red]Ollama is not running at http://localhost:11434.[/red]")
            raise typer.Exit(1)
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = config_mod.EVALUATIONS_DIR / f"quality-{stamp}.json"
    try:
        try:
            report = quality_mod.collect_evidence(
                models,
                scan_hardware(),
                pack_path=pack,
                speed_runs=speed_runs,
                confirm_performance_timeout=confirm_performance_timeout,
            )
            quality_mod.write_evidence(report, output)
        except quality_mod.QualityEvaluationError as error:
            err_console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from error

        successes = [m for m in report["models"] if m.get("outcome", "success") == "success"]
        model_unfit = [m for m in report["models"] if m.get("outcome") == "model_unfit"]
        performance_unfit = [m for m in report["models"] if m.get("outcome") == "performance_unfit"]
        transient = [m for m in report["models"] if m.get("outcome") == "transient_error"]

        if successes:
            table = Table(title="Localfit reproducible quality evidence")
            table.add_column("Model", style="cyan")
            table.add_column("Parameters")
            table.add_column("Quantization")
            table.add_column("Quality", justify="right")
            table.add_column("Speed", justify="right")
            for model in successes:
                model_quality = model["quality"]
                table.add_row(
                    str(model["tag"]),
                    str(model.get("parameter_size") or "unknown"),
                    str(model.get("quantization_level") or "unknown"),
                    (
                        f"{model_quality['correct']}/{model_quality['total']} "
                        f"({model_quality['accuracy'] * 100:.1f}%)"
                    ),
                    f"{model['speed']['median_tokens_per_sec']:.1f} tok/s",
                )
            console.print(table)

        for entry in model_unfit:
            err_console.print(
                f"[yellow]{entry['tag']}: doesn't fit this hardware "
                f"({entry.get('failure_reason', 'unknown')})[/yellow]"
            )
        for entry in performance_unfit:
            err_console.print(
                f"[red]{entry['tag']}: confirmed twice that generation exceeds the "
                f"timeout on this hardware - performance_unfit "
                f"({entry.get('failure_reason', 'unknown')})[/red]"
            )
        for entry in transient:
            err_console.print(
                f"[yellow]{entry['tag']}: temporary error, not a hardware verdict "
                f"({entry.get('failure_reason', 'unknown')})[/yellow]"
            )

        console.print(f"[green]Saved reproducible local evidence to {output}.[/green]")
        console.print(
            "[dim]No generated text is stored. v6/v7 telemetry includes CPU model, "
            "architecture, and core counts; it excludes GPU names. "
            "aggregate numbers may be shared below. Not a leaderboard.[/dim]"
        )
        if _resolve_upload_decision(
            "Send these benchmark results to the server to help train the recommendation model?"
        ):
            registry_entries = registry.load_registry()
            for model in successes:
                entry = next(
                    (e for e in registry_entries.values() if e.get("ollama_name") == model["tag"]),
                    None,
                )
                samples = model["speed"]["samples_tokens_per_sec"]
                _report_telemetry(
                    model["tag"],
                    entry.get("repo_id") if entry else None,
                    model["speed"]["median_tokens_per_sec"],
                    size_bytes=model.get("size_bytes"),
                    sample_count=model["speed"]["runs"],
                    speed_min=min(samples),
                    speed_max=max(samples),
                    quality={
                        "pack_id": report["pack"]["id"],
                        "pack_version": report["pack"]["version"],
                        "correct": model["quality"]["correct"],
                        "total": model["quality"]["total"],
                        "accuracy": model["quality"]["accuracy"],
                    },
                    model_metadata=model,
                    runtime=model.get("runtime"),
                    engine_version=report.get("environment", {}).get("engine_version"),
                    model_filename=(entry or {}).get("filename") or model["tag"],
                    model_digest=model.get("digest"),
                    provider=entry.get("provider") if entry else None,
                )
            for entry in model_unfit + performance_unfit + transient:
                _report_failure_telemetry(entry, report.get("environment", {}))

        console.print(
            f"[bold]Summary:[/bold] {len(successes)} succeeded, "
            f"{len(model_unfit)} model_unfit, {len(performance_unfit)} performance_unfit, "
            f"{len(transient)} transient_error",
            highlight=False,
        )
        if json_output:
            console.print_json(data=report)
        if not successes:
            raise typer.Exit(1)
    finally:
        if started_daemon is not None:
            benchmark.stop_ollama_daemon(started_daemon)


def _report_telemetry(
    filename: str,
    repo_id: str | None,
    tokens_per_sec: float | None,
    *,
    size_bytes: int | None = None,
    sample_count: int = 1,
    speed_min: float | None = None,
    speed_max: float | None = None,
    quality: dict | None = None,
    model_metadata: dict | None = None,
    runtime: dict | None = None,
    engine_version: str | None = None,
    model_filename: str | None = None,
    model_digest: str | None = None,
    provider: str | None = None,
) -> bool:
    if tokens_per_sec is None:
        # Ollama daemon wasn't reachable - not a real "it doesn't run" signal,
        # so skip rather than polluting the speed-regression training data.
        telemetry.log_attempt("skipped_daemon_unreachable", filename)
        console.print(
            "[dim]Telemetry not sent - Ollama daemon wasn't reachable during benchmark.[/dim]"
        )
        return False
    info = scan_hardware()
    if size_bytes is None:
        model_file = MODELS_DIR / filename
        size_bytes = model_file.stat().st_size if model_file.exists() else None
    event = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "gpu_tflops": info.gpu_tflops,
        "model_installed": filename,
        "model_repo_id": repo_id,
        "model_provider": provider or "huggingface",
        "model_size_bytes": size_bytes,
        "engine": "ollama",
        "benchmark_version": 4,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "tokens_per_sec": round(tokens_per_sec, 2),
        "sample_count": sample_count,
        "tokens_per_sec_min": round(speed_min if speed_min is not None else tokens_per_sec, 2),
        "tokens_per_sec_max": round(speed_max if speed_max is not None else tokens_per_sec, 2),
    }
    if quality is not None:
        event.update(
            quality_pack_id=quality["pack_id"],
            quality_pack_version=quality["pack_version"],
            quality_correct=quality["correct"],
            quality_total=quality["total"],
            quality_accuracy=quality["accuracy"],
        )
    metadata = model_metadata or {}

    def _number(*keys: str) -> float | None:
        for key in keys:
            value = metadata.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
            ):
                return float(value)
        return None

    candidate = {
        "name": filename,
        "filename": model_filename or filename,
        "repo_id": repo_id,
        "size_bytes": size_bytes,
    }
    parameter_count = _number("parameter_count_b", "parameter_count_billions")
    if parameter_count is None:
        value = metadata.get("parameter_size")
        parameter_count = parse_param_count_billions(value) if isinstance(value, str) else None
    if parameter_count is None:
        parameter_count = candidate_parameter_count_billions(candidate)
    active_parameter_count = _number("active_parameter_count_b", "active_parameter_count_billions")
    if active_parameter_count is None:
        active_parameter_count = candidate_active_parameter_count_billions(candidate)
    if active_parameter_count is None:
        active_parameter_count = parameter_count
    quant_bits = _number("quant_bits")
    if quant_bits is None:
        value = metadata.get("quantization_level")
        quant_bits = parse_quant_bits(value) if isinstance(value, str) else None
    if quant_bits is None:
        quant_bits = candidate_quant_bits(candidate)
    digest = _normalize_model_digest(model_digest or metadata.get("digest"))
    safe_filename = _safe_model_filename(model_filename or filename)
    complete_runtime = _complete_runtime(runtime)
    complete_cpu = _complete_cpu_metadata(info)
    client_version = _client_version()
    if (
        parameter_count is not None and active_parameter_count is not None and quant_bits is not None
        and complete_runtime is not None and complete_cpu is not None
        and isinstance(engine_version, str) and engine_version
        and client_version is not None and sample_count >= 3
    ):
        # v7: same direct-metadata contract as the old v6 promotion, plus an
        # explicit outcome so this measurement is unambiguously distinct
        # from a v7 model_unfit/transient_error failure event (never sent
        # from this function - see _report_failure_telemetry). Do not send
        # v6 from new code: v6 stays a read-only, backward-compatible
        # schema for historical data already in Firebase.
        event.update(
            parameter_count_b=parameter_count,
            active_parameter_count_b=active_parameter_count,
            quant_bits=quant_bits,
            engine_version=engine_version,
            client_version=client_version,
            benchmark_version=7,
            outcome="success",
            **complete_runtime,
            **complete_cpu,
        )
        if safe_filename is not None:
            event["model_filename"] = safe_filename
        if digest is not None:
            event["model_digest"] = digest
    sent = telemetry.send_event(event, force=True)
    if not sent:
        console.print("[dim]Telemetry not sent (will retry next time you run omm).[/dim]")
    return sent


def _report_failure_telemetry(model: dict, environment: dict) -> bool:
    """Upload a v7 model_unfit/performance_unfit/transient_error event.

    Never sends tokens_per_sec, sample_count, or any speed field - a failed
    benchmark has no real measurement, and schema/tests/model_quality_gate.py
    rely on that absence to keep this out of the speed-regression dataset.
    See docs/telemetry-v7.md for the full contract.
    """
    outcome = model.get("outcome")
    reason = model.get("failure_reason")
    if outcome not in ("model_unfit", "transient_error", "performance_unfit") or not isinstance(reason, str):
        return False
    if outcome == "performance_unfit":
        # Only ever upload a well-formed confirmation verdict: exactly 2
        # attempts and a real, positive timeout value. Anything else means
        # the confirmation flow didn't actually run as designed - drop it
        # rather than send a malformed event the Rules would reject anyway.
        attempts = model.get("confirmation_attempts")
        timeout_seconds = model.get("timeout_seconds")
        if (
            attempts != 2
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 3600
        ):
            return False
    info = scan_hardware()
    tag = model.get("tag")
    event: dict = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "model_installed": _safe_model_filename(tag) or str(tag)[:512],
        "engine": "ollama",
        "benchmark_version": 7,
        "outcome": outcome,
        "failure_reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if outcome == "performance_unfit":
        event["confirmation_attempts"] = attempts
        event["timeout_seconds"] = timeout_seconds
    engine_version = environment.get("engine_version")
    if isinstance(engine_version, str) and engine_version:
        event["engine_version"] = engine_version
    client_version = _client_version()
    if client_version:
        event["client_version"] = client_version
    complete_cpu = _complete_cpu_metadata(info)
    if complete_cpu:
        event.update(complete_cpu)

    # Best-effort model metadata: present whenever the failure happened after
    # /api/show succeeded (e.g. an out-of-memory load), absent when the model
    # couldn't even be looked up (e.g. not installed).
    metadata = model.get("model_metadata") or {}

    def _number(*keys: str) -> float | None:
        for key in keys:
            value = metadata.get(key)
            if (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0
            ):
                return float(value)
        return None

    candidate = {
        "name": tag,
        "filename": tag,
        "repo_id": None,
        "size_bytes": metadata.get("size_bytes"),
    }
    parameter_count = _number("parameter_count_b")
    if parameter_count is None:
        value = metadata.get("parameter_size")
        parameter_count = parse_param_count_billions(value) if isinstance(value, str) else None
    if parameter_count is None:
        parameter_count = candidate_parameter_count_billions(candidate)
    quant_bits = _number("quant_bits")
    if quant_bits is None:
        value = metadata.get("quantization_level")
        quant_bits = parse_quant_bits(value) if isinstance(value, str) else None
    if quant_bits is None:
        quant_bits = candidate_quant_bits(candidate)
    active_parameter_count = candidate_active_parameter_count_billions(candidate)
    if active_parameter_count is None:
        active_parameter_count = parameter_count
    if parameter_count is not None:
        event["parameter_count_b"] = parameter_count
    if active_parameter_count is not None:
        event["active_parameter_count_b"] = active_parameter_count
    if quant_bits is not None:
        event["quant_bits"] = quant_bits
    if isinstance(metadata.get("size_bytes"), int) and metadata["size_bytes"] > 0:
        event["model_size_bytes"] = metadata["size_bytes"]

    # The runtime omm *attempted* (chosen before the model failed to load),
    # not a live introspection - a model that never loaded can't be found
    # in /api/ps. Only attach it when every field is well-formed.
    attempted_runtime = model.get("attempted_runtime")
    if isinstance(attempted_runtime, dict):
        fields = ("context_length", "gpu_offload_percent", "cpu_threads", "num_batch")
        if all(
            isinstance(attempted_runtime.get(key), int) and not isinstance(attempted_runtime[key], bool)
            for key in fields
        ):
            event.update({key: attempted_runtime[key] for key in fields})
            event["runtime_profile"] = "explicit_ollama_options"

    sent = telemetry.send_event(event, force=True)
    if not sent:
        console.print(f"[dim]Telemetry not sent for {tag} (will retry next time you run omm).[/dim]")
    return sent


def _normalize_model_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    import re
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def _safe_model_filename(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 300:
        return None
    # Ollama registry tags are allowed, while local paths are reduced to a basename.
    return Path(value.replace("\\", "/")).name


def _complete_cpu_metadata(info: HardwareInfo) -> dict[str, str | int] | None:
    """Return direct-metadata (v6/v7) CPU data only when it is useful for training."""
    model = getattr(info, "cpu", None)
    arch = getattr(info, "cpu_arch", None)
    physical = getattr(info, "cpu_physical_cores", None)
    logical = getattr(info, "cpu_logical_cores", None)
    if not isinstance(model, str) or not isinstance(arch, str):
        return None
    model, arch = model.strip(), arch.strip()
    if (
        not model or not arch or len(model) > 256 or len(arch) > 64
        or model.lower() == arch.lower()
        or not isinstance(physical, int) or not isinstance(logical, int)
        or not 1 <= physical <= logical <= 1024
    ):
        return None
    return {
        "cpu_model": model,
        "cpu_arch": arch,
        "cpu_physical_cores": physical,
        "cpu_logical_cores": logical,
    }


def _complete_runtime(runtime: object) -> dict | None:
    if not isinstance(runtime, dict) or runtime.get("runtime_profile") != "explicit_ollama_options":
        return None
    fields = ("context_length", "gpu_offload_percent", "cpu_threads", "num_batch")
    if not all(isinstance(runtime.get(key), int) and not isinstance(runtime[key], bool) for key in fields):
        return None
    if runtime["context_length"] <= 0 or runtime["cpu_threads"] <= 0 or runtime["num_batch"] <= 0:
        return None
    if not 0 <= runtime["gpu_offload_percent"] <= 100:
        return None
    return {key: runtime[key] for key in fields} | {"runtime_profile": "explicit_ollama_options"}


def _client_version() -> str | None:
    return _omm_version()


@dataclass
class _ContributionStats:
    benchmarked: list[tuple[str, float]]
    skipped_unfit: int = 0
    attempted_not_uploaded: int = 0


def _telemetry_row_count(endpoint: str) -> int | None:
    """Best-effort read of how many rows exist in the (read-open) Firebase
    telemetry endpoint, for `omm contribute`'s before/after summary."""
    import requests

    try:
        resp = requests.get(f"{endpoint}?shallow=true", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return len(data) if isinstance(data, dict) else 0
    except (requests.RequestException, ValueError):
        return None


class _EscListener:
    """Background key-listener so Esc can interrupt `omm contribute` even
    mid-download/mid-benchmark, not just at a questionary prompt. No-ops
    (Ctrl+C is still the fallback) when stdin isn't a real terminal - tests,
    CI, and piped input all fall into this path, mirroring session_cache.py's
    tty-detection idiom."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import os

            os.ttyname(0)
        except (OSError, AttributeError):
            # AttributeError: os.ttyname doesn't exist on Windows at all.
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import select

            from prompt_toolkit.input import create_input

            inp = create_input()
            with inp.raw_mode():
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([inp.fileno()], [], [], 0.1)
                    if not ready:
                        continue
                    for key_press in inp.read_keys():
                        if key_press.key == Keys.Escape:
                            self.stop_event.set()
        except Exception:
            pass  # best-effort; Ctrl+C still works as a fallback


def _run_contribution_loop(
    queue, stop_event: threading.Event, refetch, quality_pack: dict | None = None
) -> _ContributionStats:
    stats = _ContributionStats(benchmarked=[])
    while not stop_event.is_set():
        candidate = queue.next_candidate(refetch=refetch)
        if candidate is None:
            console.print("[dim]No more candidates available for this hardware.[/dim]")
            break

        provider = candidate.get("provider") or "huggingface"
        resolved = ResolvedModel(
            url=download_url(provider, candidate["repo_id"], candidate["filename"]),
            filename=candidate["filename"],
            repo_id=candidate["repo_id"],
            provider=provider,
        )
        display_name = candidate.get("name", candidate["filename"])
        console.print(f"[cyan]Trying {display_name}...[/cyan]")

        try:
            outcome = _install_impl(
                resolved,
                auto_upload=True,
                skip_unfit=True,
                stop_event=stop_event,
                use_quality_eval=True,
                quality_pack=quality_pack,
            )
        except ContributionStopped as e:
            _cleanup_incomplete_install(e.filename)
            reg = registry.load_registry()
            fn, entry = _lookup_entry(e.filename, reg)
            if entry:
                _remove_one(fn, entry)
            break
        except (DownloadError, linker.LinkError) as e:
            err_console.print(f"[yellow]Skipping {candidate['filename']}: {e}[/yellow]")
            continue

        if outcome.skipped_unfit:
            stats.skipped_unfit += 1
            continue

        reg = registry.load_registry()
        fn, entry = _lookup_entry(outcome.filename, reg)
        if entry:
            _remove_one(fn, entry)

        if outcome.tokens_per_sec is not None and outcome.telemetry_sent:
            ref_str = contribute_mod.ref(candidate)
            benchmark_history.record_benchmarked(
                ref_str,
                repo_id=outcome.repo_id,
                filename=outcome.filename,
                sha256=outcome.sha256 or "",
                tokens_per_sec=outcome.tokens_per_sec,
            )
            queue.mark_seen(ref_str)
            stats.benchmarked.append((display_name, outcome.tokens_per_sec))
        else:
            stats.attempted_not_uploaded += 1

    return stats


def _print_contribution_summary(
    stats: _ContributionStats,
    duration_seconds: float,
    before_count: int | None,
    after_count: int | None,
) -> None:
    minutes, seconds = divmod(int(duration_seconds), 60)
    console.print("=" * 70)
    console.print("[bold]omm contribute: session summary[/bold]")
    console.print(f"Duration: {minutes}m {seconds}s")
    console.print(f"Models benchmarked+uploaded: {len(stats.benchmarked)}")
    for name, tokens_per_sec in stats.benchmarked:
        console.print(f"  - {name:<40} {tokens_per_sec:.1f} tok/s")
    console.print(f"Skipped (predicted not to fit this hardware): {stats.skipped_unfit}")
    console.print(f"Attempted but not uploaded (kept for retry): {stats.attempted_not_uploaded}")
    if before_count is not None and after_count is not None:
        console.print(
            f"Global telemetry dataset: {before_count} -> {after_count} rows "
            f"({after_count - before_count:+d})"
        )
        console.print(
            "  [dim](delta may include uploads from other contributors during this session)[/dim]"
        )
    console.print("=" * 70)


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
    if policy == "never":
        err_console.print(
            "[red]omm contribute requires benchmark uploads to be enabled. "
            "Run `omm setting upload --enable` or `--ask` first.[/red]"
        )
        raise typer.Exit(1)
    if policy == "always" and not load_config().get("contribute_always_ack"):
        err_console.print(
            "[yellow]Upload policy is 'always' - every benchmark result from this "
            "and future omm contribute runs will be sent to the server without "
            "asking each time.[/yellow]"
        )
        if not yes and not _ask_confirm("Continue?"):
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        config_mod.update_config(contribute_always_ack=True)

    err_console.print(
        "[yellow]This will repeatedly download, benchmark, and delete GGUF models "
        "until you press Esc. It uses real bandwidth, disk space, and compute, "
        "runs unattended (no per-model confirmation), and uploads every benchmark "
        f"result to the server per your current upload policy ({policy}).[/yellow]"
    )
    if not yes and not _ask_confirm("Start contributing compute now?"):
        err_console.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(0)

    if not benchmark.ollama_daemon_reachable():
        err_console.print(
            "[red]omm contribute requires a running Ollama daemon - "
            "it's the only benchmarkable engine right now.[/red]"
        )
        raise typer.Exit(1)

    try:
        quality_pack, _ = quality_mod.load_pack()
    except quality_mod.QualityEvaluationError as error:
        err_console.print(f"[red]Could not load the quality pack: {error}[/red]")
        raise typer.Exit(1) from error

    config = load_config()
    artifact, _ = _load_recommendation_with_change_note(config)
    if not artifact or not artifact.get("candidates"):
        err_console.print(
            "[red]No trained recommendation model available - can't select candidates.[/red]"
        )
        raise typer.Exit(1)

    endpoint = config.get("telemetry_endpoint")
    before_count = _telemetry_row_count(endpoint) if endpoint else None

    hw = scan_hardware()
    history_refs = benchmark_history.loaded_refs()
    queue = contribute_mod.ContributionQueue(artifact, hw, history_refs)

    def refetch():
        return _load_recommendation_with_change_note(config)

    listener = _EscListener()
    listener.start()
    start_time = time.monotonic()
    try:
        stats = _run_contribution_loop(queue, listener.stop_event, refetch, quality_pack=quality_pack)
    finally:
        listener.stop_event.set()

    autoremove()

    after_count = _telemetry_row_count(endpoint) if endpoint else None
    duration = time.monotonic() - start_time
    _print_contribution_summary(stats, duration, before_count, after_count)


if __name__ == "__main__":
    app()
