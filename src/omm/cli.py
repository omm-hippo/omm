"""omm CLI entry point (apt/brew-style command routing)."""

from __future__ import annotations

import errno
import functools
import inspect
import json
import math
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.padding import Padding
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
    contribute_memory,
    contribute_state,
    doctor as doctor_mod,
    error_report,
    errors,
    fit_ui,
    launcher,
    linker,
    memory_guard as memory_guard_mod,
    onboarding,
    package_metadata,
    predictor,
    recommend_status,
    quality as quality_mod,
    recommend_ui,
    registry,
    rules as rules_mod,
    scan_import,
    search as search_mod,
    session_cache,
    telemetry,
    theme as theme_mod,
    trust,
    tuning,
    version_check,
)
from omm import contribute as contribute_mod
from omm.completion import complete_engine_key, complete_install_name, complete_remove_filename
from omm.config import MODELS_DIR, OMM_HOME, load_config, save_config
from omm.downloader import (
    DownloadCancelled,
    DownloadError,
    InsufficientDiskSpaceError,
    _sidecar_path,
    download_file,
)
from omm.engines import RuntimeAdapterError, RuntimeModelRef, find_runtime_model
from omm.engines.lmstudio import LMStudioAdapter
from omm.engines.ollama import OllamaAdapter
from omm.hardware import (
    BUSY_CPU_PERCENT,
    HardwareInfo,
    WindowsCommitInfo,
    available_ram_gb,
    calculate_memory_budget,
    sample_cpu_utilization_percent,
    scan_hardware,
    windows_commit_info,
)
from omm.hashutil import sha256_file
from omm.featurize import (
    resolve_active_parameter_count_billions,
    candidate_parameter_count_billions,
    candidate_quant_bits,
    is_mmproj_filename,
    parse_chip_score,
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
    fetch_repo_files,
    fetch_repo_param_count_b,
    model_filename_identity,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    remote_gguf_metadata,
    resolve_model,
    validate_model_filename,
    validate_provider,
    validate_repo_id,
)
from omm.runtime_compatibility import CompatibilityResult, PROBE_VERSION, verify_and_record

if TYPE_CHECKING:
    import questionary


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


def _get_current_context():
    """Return click's `get_current_context`, from whichever click Typer
    actually pushed onto. Typer >=0.16 forks click internally as
    typer._click with its own thread-local context stack; older Typer
    (the pypi-installed default at time of writing pins no upper bound)
    pushes onto the standalone `click` package's stack instead. Importing
    the wrong one means get_current_context() finds no active context and
    raises RuntimeError, or - as originally shipped here - the import
    itself raises ModuleNotFoundError on installs without typer._click."""
    try:
        from typer._click.globals import get_current_context
    except ImportError:
        from click.globals import get_current_context
    return get_current_context


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
    # True once a command body has actually started running. Click's eager
    # `--help` option prints and exits from the *sub*command's context,
    # which is created only after the root callback has run - so at close
    # time this flag is the one available signal that the user got real
    # command output rather than a help page. See
    # _update_notice_is_wanted.
    command_body_ran: bool = False


# Commands whose output --json actually restructures. Every other command
# silently ignores the flag - warn instead so a script piping --json from
# one of them doesn't get plain-text garbage with exit code 0 (see #81).
_JSON_CAPABLE = {
    "search",
    "list",
    "info",
    "benchmark",
    "tune",
    "scan",
    "recommend",
    "doctor",
    "fit",
}

# Commands with a confirmation prompt --yes/-y can skip. Every other
# command has nothing for it to do.
_YES_CAPABLE = {"install", "import", "uninstall", "upgrade", "contribute", "recommend", "benchmark"}


def _global_opts() -> GlobalOptions:
    """Read the merged GlobalOptions for the command currently running.
    Falls back to defaults when called outside an active Click/Typer
    context (e.g. a test calling an `_impl` function directly)."""
    get_current_context = _get_current_context()

    try:
        return get_current_context().ensure_object(GlobalOptions)
    except RuntimeError:
        return GlobalOptions()


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
                    "--quiet", "-q", help="Suppress progress bars and background status/hint lines (errors and results still print)."
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
        ctx = _get_current_context()()
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
        # Full path minus the root program name, not ctx.command.name alone:
        # a nested command can share its bare name with an unrelated
        # top-level one (e.g. "omm engine install" vs "omm install") - the
        # bare name would false-match _JSON_CAPABLE/_YES_CAPABLE and swallow
        # a warning the nested command actually needs.
        command_name = ctx.command_path.removeprefix("omm ")
        if opts.json and command_name not in _JSON_CAPABLE:
            err_console.print(
                f"[warning]--json has no effect on `omm {command_name}` - ignoring it.[/warning]"
            )
        if opts.yes and command_name not in _YES_CAPABLE:
            err_console.print(
                f"[warning]--yes has no effect on `omm {command_name}` - it has no confirmation prompt to skip.[/warning]"
            )
        if opts.pending_telemetry_notice and not (opts.json or opts.quiet):
            console.print(
                f"[muted]Sent {opts.pending_telemetry_notice} queued telemetry "
                "event(s) from a previous session.[/muted]"
            )
        opts.pending_telemetry_notice = 0
        opts.command_body_ran = True
        return func(*args, **kwargs)

    wrapper.__signature__ = original_sig.replace(parameters=new_params)
    return wrapper


def marks_command_body_ran(func):
    """Records that a command body started, for commands that can't take
    `global_flags` (`verify` declares its own `--yes`, which would clash
    with the one that decorator adds). `global_flags` sets the same flag
    for every command it wraps."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        _global_opts().command_body_ran = True
        return func(*args, **kwargs)

    return wrapper


_ROOT_HELP_TEXT = """Example usage:
  omm search TEXT
  omm install MODEL
  omm list
  omm recommend
  omm uninstall MODEL

Tuning & quality:
  omm tune MODEL
  omm benchmark MODEL...
  omm contribute

Maintenance:
  omm scan
  omm doctor
  omm setup
  omm engine install
  omm upgrade [MODEL]
  omm setting

Further help:
  omm help COMMAND      Show help for one command
  omm help --all        List every command
  https://github.com/omm-hippo/omm
"""

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


def _table(*args, **kwargs) -> Table:
    """Every table omm prints, in the site's hierarchy: dim rules and
    title, bold header row. Column styles stay per call site (labels are
    `label`, values `value`, filenames `accent`)."""
    kwargs.setdefault("border_style", "rule")
    kwargs.setdefault("header_style", "heading")
    kwargs.setdefault("title_style", "muted")
    return Table(*args, **kwargs)


app = typer.Typer(
    name="omm",
    help="Open source Model Manager - package manager for local LLMs (GGUF).",
    rich_markup_mode=None,
    cls=_RootHelpGroup,
)
setting_app = typer.Typer(
    name="setting",
    help="View or change omm settings (telemetry, upload policy, version, calibration, catalog trust).",
    invoke_without_command=True,
    rich_markup_mode=None,
)
app.add_typer(setting_app)
engine_app = typer.Typer(
    name="engine",
    help="Install local AI runner programs (Ollama, LM Studio, etc.).",
    rich_markup_mode=None,
)
app.add_typer(engine_app)
if platform.system() == "Windows":
    # Legacy cp949/cp1252 consoles cannot encode every model name or symbol.
    # Preserve their configured encoding but replace unsupported glyphs rather
    # than crashing a command with UnicodeEncodeError.
    for _stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(_stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
console = Console(safe_box=platform.system() == "Windows", highlight=False)
err_console = Console(stderr=True, safe_box=platform.system() == "Windows", highlight=False)

REPO_URL = "git+https://github.com/omm-hippo/omm.git"
COMPATIBLE_PROGRAMS_URL = "https://omm.run/#runners"


def _load_recommendation_with_change_note(config: dict) -> tuple[dict | None, bool]:
    manifest_url = config.get("catalog_manifest_url")
    public_key = config.get("catalog_public_key")
    if manifest_url and public_key:
        artifact, changed = predictor.load_model_with_change_note(
            config.get("model_url"), manifest_url, public_key
        )
    else:
        artifact, changed = predictor.load_model_with_change_note(config.get("model_url"))
    _handle_emergency_signal(artifact)
    return artifact, changed


def _version_at_least(installed: str, required: str) -> bool:
    """Loose dotted-integer compare (e.g. "0.2.98" >= "0.2.90"). Any
    non-numeric or otherwise unparseable component makes this return False -
    for an emergency signal, an uncertain comparison should nag rather than
    silently stay quiet."""

    def _parts(v: str) -> tuple[int, ...] | None:
        try:
            return tuple(int(p) for p in v.strip().split("."))
        except ValueError:
            return None

    installed_parts, required_parts = _parts(installed), _parts(required)
    if installed_parts is None or required_parts is None:
        return False
    width = max(len(installed_parts), len(required_parts))
    installed_parts += (0,) * (width - len(installed_parts))
    required_parts += (0,) * (width - len(required_parts))
    return installed_parts >= required_parts


_emergency_signals_shown: set[str] = set()


def _restart_after_update() -> None:
    """Re-exec the current process with the same argv so the update just
    installed by _handle_emergency_signal takes effect immediately, picking
    up the interrupted command right where the emergency prompt cut in."""
    try:
        os.execv(sys.argv[0], sys.argv)
    except OSError as e:
        err_console.print(
            f"[error]Updated, but could not restart automatically ({e}). "
            "Please re-run your last command.[/error]"
        )
        raise typer.Exit(1) from e


def _handle_emergency_signal(artifact: dict | None) -> None:
    """Every command that fetches the recommendation model (recommend,
    search, contribute) runs this right after: if the model - already
    Ed25519-verified in fetch_and_cache_model - carries a signed `emergency`
    field aimed at versions older than this install, block instead of
    quietly continuing. This is the last-resort channel for a critical
    omm/Firebase/runner incompatibility: no separate network call, it rides
    the model fetch that already happens routinely.

    Blocking means exactly that: the only way past this function without
    raising typer.Exit is updating (which restarts the process instead of
    returning) or the signal not applying (absent, already fixed locally,
    or already shown once this run). A non-interactive caller or a declined
    prompt both still exit non-zero - continuing to run against a publisher-
    declared-critical incompatibility silently is the one outcome this
    feature exists to prevent.

    Deliberately not wired to any publishing path yet - publishing an
    `emergency` block still has to go through the existing CI signing step
    (scripts/sign_catalog.py) by hand. This only teaches omm to react."""
    signal = predictor.extract_emergency_signal(artifact)
    if signal is None:
        return

    signal_id = signal.get("id") or signal["message"]
    if signal_id in _emergency_signals_shown:
        return

    fixed_in_version = signal.get("fixed_in_version")
    if fixed_in_version and _version_at_least(_omm_version(), fixed_in_version):
        return

    _emergency_signals_shown.add(signal_id)
    err_console.print(f"[error]⚠ EMERGENCY: {escape(signal['message'])}[/error]")

    if not _stdin_is_tty():
        err_console.print(
            "[error]Not an interactive terminal - can't ask, so refusing to continue. "
            "Run `omm update` and try again.[/error]"
        )
        raise typer.Exit(1)
    if not _ask_confirm("Update omm now to resolve this?", default=True):
        err_console.print(
            "[error]Not updating - refusing to continue against a declared-critical "
            "issue. Run `omm update` when ready.[/error]"
        )
        raise typer.Exit(1)

    branch = _channel_branch()
    result = _perform_update(branch)
    if result.returncode != 0:
        err_console.print(f"[error]Emergency update failed:[/error]\n{result.stderr}")
        raise typer.Exit(1)
    console.print("[success]Updated. Restarting the interrupted command...[/success]")
    _restart_after_update()


def _omm_version() -> str:
    """Reads the freshly-pulled SRC_DIR/pyproject.toml when this is a
    migrated editable install: dist-info is frozen at the last full `pipx
    install` (see _deps_satisfied's docstring), so importlib.metadata would
    keep reporting a stale version after every git-pull-only `omm update`
    even though the commit hash and code have moved on."""
    if (
        package_metadata.install_source() is package_metadata.InstallSource.GIT
        and _editable_install_uses_src()
    ):
        try:
            text = (SRC_DIR / "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            text = None
        if text is not None:
            match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
            if match:
                return match.group(1)

    try:
        return package_metadata.version()
    except Exception:
        return "dev"


def _version_line(commit: str | None) -> str:
    """'0.1.23 (a1b2c3d, beta)' style summary, shared by the bare `omm`
    banner and `omm update`'s before/after display."""
    parts = [commit[:7]] if commit else []
    parts.append(_update_channel())
    return f"{_omm_version()} ({', '.join(parts)})"


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
def _root(
    ctx: typer.Context,
    version_flag: Annotated[
        bool,
        typer.Option("--version", help="Show the installed OMM version and exit.", is_eager=True),
    ] = False,
    json_flag: Annotated[bool, typer.Option("--json", help="Print output as JSON where supported.")] = False,
    yes_flag: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts. For scripting.")] = False,
    quiet_flag: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress progress bars and background status/hint lines (errors and results still print).")] = False,
    no_color_flag: Annotated[bool, typer.Option("--no-color", help="Disable colored output.")] = False,
) -> None:
    if version_flag:
        typer.echo(f"omm {_omm_version()}")
        raise typer.Exit(0)
    opts = ctx.ensure_object(GlobalOptions)
    opts.json = opts.json or json_flag
    opts.yes = opts.yes or yes_flag
    opts.quiet = opts.quiet or quiet_flag
    opts.no_color = opts.no_color or no_color_flag
    side_effect_minimal_mode = ctx.invoked_subcommand == "doctor"
    theme = (
        doctor_mod.read_theme_read_only()
        if side_effect_minimal_mode
        else load_config().get("theme", "dark")
    )
    theme_mod.apply_theme_to_console(console, theme)
    theme_mod.apply_theme_to_console(err_console, theme)
    if opts.no_color:
        console.no_color = True
        err_console.no_color = True
    # `omm doctor` promises a literal read-only snapshot. The normal root
    # prelude can create/migrate config, spawn an update checker, offer to
    # import models, and flush queued network events, so return directly
    # before any of those hooks are reached.
    if side_effect_minimal_mode:
        return
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        # Bare `omm` prints a real (if short) result, so it counts as a
        # command body for the update notice - no subcommand will run to
        # set the flag itself.
        opts.command_body_ran = True
        _maybe_run_onboarding(ctx)
        console.print(f"Ω omm {_version_line(_installed_commit())}")
        console.print(f"[muted]{_telemetry_destination_line()}[/muted]")
        raise typer.Exit(0)
    _maybe_run_onboarding(ctx)
    _maybe_auto_import(ctx)
    # A setting command may revoke consent or change the destination, so do
    # not send queued telemetry before the requested mutation takes effect.
    if ctx.invoked_subcommand != "setting":
        resent = telemetry.flush_pending()
        if resent and not (opts.json or opts.quiet):
            err_console.print(
                f"[muted]Sent {resent} queued telemetry event(s) "
                "from a previous session.[/muted]"
            )
        # Only an `always` policy flushes unattended here; under `ask` the
        # queue deliberately waits for the next `omm contribute`, which is
        # the one place the user is asked about error reports.
        reported = error_report.flush_pending()
        if reported and not (opts.json or opts.quiet):
            err_console.print(
                f"[muted]Sent {reported} queued error report(s) "
                "from a previous session.[/muted]"
            )


_HELP_ALL_GROUPS: list[tuple[str, list[str]]] = [
    ("Core", ["search", "install", "run", "fit", "verify", "list", "recommend", "uninstall", "info", "upgrade"]),
    ("Tuning & quality", ["tune", "benchmark", "contribute"]),
    (
        "Maintenance",
        [
            "scan",
            "doctor",
            "setup",
            "engine",
            "import",
            "autoremove",
            "cleanup",
            "link",
            "update",
            "help",
        ],
    ),
]


def _command_reference_grid(name_width: int) -> Table:
    """Two-column layout for one `omm help --all` section. A summary too
    long for the terminal wraps under the summary column; the padded
    f-string this replaces let it restart at column 0, where the
    continuation read as if it belonged to no command at all.

    `name_width` is fixed by the caller rather than left to the grid so
    that every top-level section shares one summary column, the way the
    single global width used to line them all up."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, width=name_width)
    grid.add_column()
    return grid


def _add_command_row(grid: Table, name: str, cmd_obj: click.Command) -> None:
    grid.add_row(f"  omm {name}", cmd_obj.get_short_help_str(limit=1000))


def _print_command_flags(root_ctx: click.Context, name: str, cmd_obj: click.Command) -> None:
    """Indented flag block for one command, used by `help --all --flags`.
    Built from each param's own `get_help_record` (name + help only)
    rather than `get_help()`, which would repeat the Usage/description
    lines already shown by the summary grid above."""
    sub_ctx = cmd_obj.make_context(name, [], parent=root_ctx, resilient_parsing=True)
    # Typer's vendored click fork gives positional Arguments a
    # get_help_record() too (vanilla click.Argument returns None) - filter
    # those out by dash-prefix so this block only lists actual flags.
    records = [
        record
        for p in cmd_obj.params
        if (record := p.get_help_record(sub_ctx)) is not None and record[0].startswith("-")
    ]
    if not records:
        return
    console.print(f"  [bold]omm {name}[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for opts, help_text in records:
        grid.add_row(f"    {opts}", help_text)
    console.print(grid)


def _print_full_command_reference(root_ctx: click.Context, show_flags: bool = False) -> None:
    """Homebrew/git-`-a`-style listing: every non-hidden command plus its
    one-line summary, grouped like the curated root help. `show_flags`
    (the `--flags` option) additionally expands each command's option
    list beneath its row - `omm <command> --help` still exists for a
    single command's full formatted help text."""
    commands = root_ctx.command.commands
    grouped_names = {name for _, names in _HELP_ALL_GROUPS for name in names}
    name_width = len("  omm ") + max(
        (len(n) for n in commands if not commands[n].hidden), default=0
    )

    for title, names in _HELP_ALL_GROUPS:
        visible = [n for n in names if n in commands and not commands[n].hidden]
        if not visible:
            continue
        console.print(f"[bold]{title}:[/bold]")
        grid = _command_reference_grid(name_width)
        for name in visible:
            _add_command_row(grid, name, commands[name])
        console.print(grid)
        if show_flags:
            for name in visible:
                _print_command_flags(root_ctx, name, commands[name])
        console.print()

    setting_cmd = commands.get("setting")
    if setting_cmd is not None and hasattr(setting_cmd, "commands"):
        # Typer >=0.16 vendors its own click fork (typer._click), so
        # TyperGroup subclasses that fork's Command directly rather than
        # the standalone `click` package's Group/MultiCommand -
        # isinstance(setting_cmd, click.Group) silently never matches.
        # Duck-type on the `.commands` dict every group (real or vendored)
        # exposes instead.
        sub_names = [n for n in sorted(setting_cmd.commands) if not setting_cmd.commands[n].hidden]
        if sub_names:
            console.print("[bold]Settings (omm setting SUBCOMMAND):[/bold]")
            console.print(Padding(setting_cmd.help, (0, 0, 0, 2)))
            grid = _command_reference_grid(
                len("  omm setting ") + max(len(n) for n in sub_names)
            )
            for name in sub_names:
                _add_command_row(grid, f"setting {name}", setting_cmd.commands[name])
            console.print(grid)
            if show_flags:
                for name in sub_names:
                    _print_command_flags(root_ctx, f"setting {name}", setting_cmd.commands[name])
            console.print()

    # Safety net: any top-level command not yet slotted into a group above
    # (e.g. a newly added one) still shows up here instead of vanishing.
    leftover = sorted(
        n for n in commands if n not in grouped_names and n != "setting" and not commands[n].hidden
    )
    if leftover:
        console.print("[bold]Other:[/bold]")
        grid = _command_reference_grid(name_width)
        for name in leftover:
            _add_command_row(grid, name, commands[name])
        console.print(grid)
        if show_flags:
            for name in leftover:
                _print_command_flags(root_ctx, name, commands[name])
        console.print()

    console.print("[muted]Run `omm COMMAND --help` for a command's full option list.[/muted]")
    if not show_flags:
        console.print("[muted]Add `--flags` to show every command's options inline.[/muted]")
    console.print("[muted]Exit codes: 0 success, 1 failure, 2 usage error (bad flag/argument).[/muted]")


@app.command(name="help")
@global_flags
def help_cmd(
    ctx: typer.Context,
    command: str = typer.Argument(None, help="Show help for a specific subcommand."),
    all: bool = typer.Option(False, "--all", help="List every command, not just the common ones."),
    flags: bool = typer.Option(
        False, "--flags", help="With --all, also show each command's full option list."
    ),
) -> None:
    """Show help, same as --help."""
    root_ctx = ctx.find_root()
    if command is None:
        if all:
            _print_full_command_reference(root_ctx, show_flags=flags)
            raise typer.Exit(0)
        console.print(root_ctx.get_help())
        raise typer.Exit(0)

    cmd_obj = root_ctx.command.get_command(root_ctx, command)
    if cmd_obj is None:
        err_console.print(f"[error]No such command '{command}'. See `omm help`.[/error]")
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


def _managed_model_path(filename: str) -> Path:
    """Resolve a registry/CLI filename inside the central model hub."""
    filename = validate_model_filename(filename)
    root = MODELS_DIR.resolve()
    candidate = MODELS_DIR / filename
    if not candidate.resolve().is_relative_to(root):
        raise ModelResolutionError(
            f"model filename escapes the managed model hub: {filename}"
        )
    identity = model_filename_identity(filename)
    for registered_filename in registry.load_registry():
        if registered_filename == filename:
            continue
        try:
            registered_identity = model_filename_identity(registered_filename)
        except ModelResolutionError:
            registered_identity = None
        if registered_identity is not None and registered_identity == identity:
            raise ModelResolutionError(
                f"model filename collides with registered path "
                f"{registered_filename!r} on a case-insensitive filesystem"
            )
        if not isinstance(registered_filename, str):
            continue
        registered_path = MODELS_DIR / registered_filename
        try:
            registered_is_managed = registered_path.resolve().is_relative_to(root)
        except OSError:
            registered_is_managed = False
        if registered_is_managed and candidate.exists() and registered_path.exists():
            try:
                aliases_existing_path = candidate.samefile(registered_path)
            except OSError:
                aliases_existing_path = False
            if aliases_existing_path:
                raise ModelResolutionError(
                    f"model filename aliases registered path "
                    f"{registered_filename!r} on this filesystem"
                )
    return candidate


def _link_repair_needed(reg: dict) -> bool:
    """True if some omm-hub model isn't yet symlinked into an installed
    engine (e.g. Ollama/LM Studio was installed after the model was).
    Engines already recorded as blocked by a prior `omm link` attempt
    (e.g. an unowned Ollama manifest) are excluded - `omm link` itself
    still retries them every run, but the scan nag would otherwise repeat
    forever for a conflict the user can't resolve by re-running it."""
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    for filename, entry in reg.items():
        try:
            model_path = _managed_model_path(filename)
        except ModelResolutionError:
            continue
        if not model_path.exists():
            continue
        linked = entry.get("linked", {})
        blocked = entry.get("link_blocked") or []
        for key, is_installed in installed.items():
            if is_installed and not linked.get(key) and key not in blocked:
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


def _validate_engine(engine: str | None) -> None:
    """Shared `--engine` check for `list`/`link`: exits 2 with a usage
    error when a value is given but isn't a known engine key."""
    if engine is None:
        return
    valid_engines = {spec.key for spec in linker.ENGINES}
    if engine not in valid_engines:
        err_console.print(
            f"[error]--engine must be one of: {', '.join(sorted(valid_engines))} (got '{engine}').[/error]"
        )
        raise typer.Exit(2)


def _missing_engines_note(installed: dict[str, bool]) -> str | None:
    """One-line pointer to the compatibility wiki page for engines not
    installed on this machine - `None` when every known engine is
    installed, so info/scan tables don't print a useless zero-count line."""
    missing = sum(1 for is_installed in installed.values() if not is_installed)
    if missing == 0:
        return None
    return f"+ {missing} program(s) not installed — see the compatibility list: {COMPATIBLE_PROGRAMS_URL}"


def _positive_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _hub_storage_bytes(reg: dict[str, Any]) -> int:
    """Total on-disk size of every omm-hub-managed model. Prefers each
    entry's stored `size_bytes` and falls back to a live stat() when it's
    missing/invalid (same fallback used for Memory Guard's own size check
    above) or the file is gone (then it just contributes 0)."""
    total = 0
    for filename, entry in reg.items():
        size_bytes = entry.get("size_bytes")
        if not _positive_finite_number(size_bytes):
            try:
                size_bytes = _managed_model_path(filename).stat().st_size
            except (ModelResolutionError, OSError):
                size_bytes = 0
        total += int(size_bytes)
    return total


@app.command()
@global_flags
def scan() -> None:
    """Scan current PC hardware (RAM, VRAM, OS) and print a summary table."""
    opts = _global_opts()
    info = scan_hardware()
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    reg = registry.load_registry()
    cleaned = _reconcile_stale_link_records(reg, installed)
    external = scan_import.find_external_models()
    hub_storage_gb = _hub_storage_bytes(reg) / (1024**3)
    storage_saved_gb = load_config().get("storage_saved_bytes", 0) / (1024**3)

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
                "hub_storage_gb": hub_storage_gb,
                "storage_saved_gb": storage_saved_gb,
                "engines_installed": [spec.key for spec in linker.ENGINES if installed[spec.key]],
                "stale_links": cleaned,
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

    table = _table(title="omm hardware scan")
    table.add_column("Field", style="label")
    table.add_column("Value", style="value")

    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    table.add_row("RAM (available)", f"{info.ram_available_gb:.1f} GB")
    budget = calculate_memory_budget(info)
    table.add_row("Safe model budget now", f"{budget.model_budget_gb:.1f} GB")
    table.add_row("Reserved for apps/OS", f"{budget.ram_safety_reserve_gb:.1f} GB+")
    table.add_row("omm hub storage", f"{hub_storage_gb:.1f} GB")
    table.add_row("Saved via omm import", f"{storage_saved_gb:.1f} GB")

    if info.unified_memory:
        table.add_row("Memory type", "Unified (Apple Silicon)")
        table.add_row("GPU", info.gpu_name or "Unknown")
    elif info.gpu_name:
        table.add_row("GPU", info.gpu_name)
        if info.vram_total_gb is not None:
            table.add_row("VRAM (total)", f"{info.vram_total_gb:.1f} GB")
        if info.vram_free_gb is not None:
            table.add_row("VRAM (free)", f"{info.vram_free_gb:.1f} GB")
        if info.vram_total_gb is None:
            table.add_row("VRAM", "Shared or unavailable from the OS")
    else:
        table.add_row("GPU", "None detected")

    console.print(table)

    engine_table = _table(title="Local AI runners", box=None)
    engine_table.add_column("Program", style="label")
    engine_table.add_column("Status", style="success")
    for spec in linker.ENGINES:
        if installed[spec.key]:
            engine_table.add_row(spec.label, "installed")
    console.print()
    console.print(engine_table)
    note = _missing_engines_note(installed)
    if note and not opts.quiet:
        console.print(note, style="muted")

    model_table = _table(title="Local AI models", box=None)
    # Model names get the room first: a truncated `tinyllama-1.1b-cha…` is
    # what the user has to type back, the path is only where it lives.
    model_table.add_column("Model", style="accent", overflow="ellipsis", min_width=30)
    model_table.add_column("Location", style="value", overflow="ellipsis")
    model_table.add_column("Engine(s)", style="muted")
    model_table.add_column("Managed by omm", style="muted", no_wrap=True)
    for filename, entry in reg.items():
        linked = entry.get("linked", {})
        engines = [name for name, on in linked.items() if on]
        model_table.add_row(filename, "(omm hub)", ", ".join(engines) or "-", "yes")
    for item in external:
        model_table.add_row(item.display_name, _shorten_home(item.path), item.engine, "no")
    console.print()
    console.print(model_table)

    if opts.quiet:
        return
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


@app.command(name="setup")
@global_flags
def setup_cmd() -> None:
    """Re-run the first-time setup wizard (hardware scan + engine checklist)."""
    onboarding.run_wizard(console)
    config_mod.update_config(onboarding_completed=True)


@engine_app.command(name="install")
@global_flags
def engine_install_cmd(
    engine: str = typer.Argument(
        None,
        autocompletion=complete_engine_key,
        help="Engine key to install directly, skipping the checklist "
        "(ollama, lmstudio, jan, anythingllm, mstystudio, textgenwebui, koboldcpp).",
    ),
) -> None:
    """Install a local AI runner program (Ollama, LM Studio, etc.).

    With no argument, interactively pick from a checklist. With an engine
    key, install that one runner directly."""
    if engine is None:
        selected = onboarding.run_engine_checklist(console)
        if selected is None:
            raise typer.Abort()
        if selected and not onboarding.install_selected_engines(console, selected):
            raise typer.Exit(1)
        return

    key = engine.strip().lower()
    valid_engines = {spec.key for spec in linker.ENGINES}
    if key not in valid_engines:
        err_console.print(
            f"[error]engine must be one of: {', '.join(sorted(valid_engines))} (got '{engine}').[/error]"
        )
        raise typer.Exit(2)
    if linker.is_engine_installed(key):
        label = next(spec.label for spec in linker.ENGINES if spec.key == key)
        console.print(f"[muted]{label} is already installed.[/muted]")
        return
    if not onboarding.install_selected_engines(console, [key]):
        raise typer.Exit(1)


def _refresh_data() -> None:
    """Unconditionally re-fetch rules.json and recommend-model.json from
    their configured URLs (used by `omm update` for a full data sync)."""
    import requests

    config = load_config()

    rules_url = config.get("rules_url")
    if rules_url:
        try:
            fetched = rules_mod.fetch_rules(rules_url)
            console.print(f"[success]Updated rules.json ({len(fetched)} entries) from {rules_url}[/success]")
        except (requests.RequestException, ValueError) as e:
            err_console.print(f"[error]Failed to fetch rules from {rules_url}: {e}[/error]")

    model_url = config.get("model_url")
    if model_url:
        try:
            manifest_url = config.get("catalog_manifest_url")
            public_key = config.get("catalog_public_key")
            previous = predictor.load_cached_model()
            if manifest_url and public_key:
                artifact = predictor.fetch_and_cache_model(model_url, manifest_url, public_key)
            else:
                artifact = predictor.fetch_and_cache_model(model_url)
            style = "success" if artifact != previous else "muted"
            console.print(
                f"[{style}]Updated recommend-model.json "
                f"({len(artifact.get('candidates', []))} candidates) from {model_url}[/{style}]"
            )
        except (requests.RequestException, ValueError) as e:
            err_console.print(f"[error]Failed to fetch trained model from {model_url}: {e}[/error]")


_BARE_REPO_URL = REPO_URL.removeprefix("git+")

_PACKAGE_CHECKOUT = Path(__file__).resolve().parents[2]
SRC_DIR = _PACKAGE_CHECKOUT if (_PACKAGE_CHECKOUT / ".git").exists() else OMM_HOME / "src"


def _update_channel() -> str:
    """'stable' or 'beta', from `omm setting version` (config key
    update_channel). Anything else in config falls back to stable."""
    if package_metadata.install_source() is not package_metadata.InstallSource.GIT:
        return "stable"
    channel = load_config().get("update_channel")
    return "beta" if channel == "beta" else "stable"


def _channel_branch(channel: str | None = None) -> str:
    """Repo branch backing the given (or current) update channel."""
    return "beta" if (channel or _update_channel()) == "beta" else "main"


def _src_head_commit() -> str | None:
    """HEAD commit of the persistent editable clone at SRC_DIR, if this
    install has migrated to the git-pull update mechanism. None if not
    migrated yet, or if the clone is missing/corrupted (triggers
    self-healing re-migration in update())."""
    if package_metadata.install_source() is not package_metadata.InstallSource.GIT:
        return None
    if not (SRC_DIR / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(SRC_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _editable_install_uses_src(install_record: dict | None = None) -> bool:
    """True only when PEP 610 proves the installed package uses SRC_DIR.

    A cloned ``~/.omm/src`` is not proof that the currently executing pipx
    environment is editable. A failed one-time migration can leave that clone
    behind while the environment still runs an older VCS snapshot.
    """
    if install_record is None:
        try:
            install_record = package_metadata.direct_url()
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    if not isinstance(install_record, dict):
        return False
    dir_info = install_record.get("dir_info")
    if not isinstance(dir_info, dict) or dir_info.get("editable") is not True:
        return False
    raw_url = install_record.get("url")
    if not isinstance(raw_url, str):
        return False
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return False
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    raw_path = url2pathname(unquote(parsed.path))
    if platform.system() == "Windows" and re.fullmatch(r"/[A-Za-z]:.*", raw_path):
        raw_path = raw_path[1:]
    try:
        return Path(raw_path).resolve() == SRC_DIR.resolve()
    except OSError:
        return False


def _installed_commit() -> str | None:
    """The commit the installed package actually executes.

    PEP 610 identifies whether this environment is the migrated editable
    install or an older VCS snapshot. Never treat a merely-present SRC_DIR as
    installed code: it may be residue from a failed migration.
    """
    install_record = package_metadata.direct_url()
    if _editable_install_uses_src(install_record):
        return _src_head_commit()
    if not install_record:
        return None
    vcs_info = install_record.get("vcs_info", {})
    return vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None


def _remote_head_commit(ref: str = "main") -> str | None:
    """Latest commit on the given ref of the omm repo, via `git ls-remote`
    (no GitHub API rate limit, no auth needed for a public repo)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", _BARE_REPO_URL, ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _cached_remote_head_commit(ref: str = "main") -> str | None:
    return version_check.cached_remote_head(_remote_head_commit, ref)


_SKIP_UPDATE_CHECK_SUBCOMMANDS = {"update", "doctor", "help", "_bg-version-check"}


@app.command(name="_bg-version-check", hidden=True)
def _bg_version_check_cmd() -> None:
    """Internal. Spawned by `_maybe_start_update_check` as a detached child
    so the `git ls-remote` round trip survives the short-lived parent
    command exiting; writes the result to the shared cache for a later
    `omm` invocation to pick up."""
    version_check.cached_remote_head(_remote_head_commit, _channel_branch())


def _update_notice_is_wanted(opts: GlobalOptions) -> bool:
    """Whether the deferred update notice should still print by the time the
    command has finished.

    Silent under `--quiet`: this notice is exactly the "background
    status/hint line" that flag documents suppressing, and it is unrelated
    to whatever the user actually ran. Silent when no command body ran,
    which is how `omm <command> --help` gets here - Click's eager help
    option prints and exits from the subcommand's context, well after the
    root callback registered the close callback, so the notice used to land
    appended below help text the user was still reading.

    Both flags are read at close time rather than captured when the notice
    was registered, because both are set after that point: the body flag by
    the command itself, and `quiet` by global_flags() when `--quiet` came
    after the subcommand name."""
    return opts.command_body_ran and not opts.quiet


def _confirm_and_print_update_notice(cached_latest: str, installed: str, branch: str = "main") -> None:
    """The cached remote head can be up to _TTL_SECONDS stale, so a mismatch
    against it is only a hint, not proof. Before alarming the user, re-check
    live and refresh the cache - this trades a bit of extra latency (only on
    the rare command where the notice would otherwise fire) for never showing
    a stale "update available" once the real remote has caught up."""
    latest = _remote_head_commit(branch)
    if latest is None:  # offline/unreachable - don't guess, stay silent
        return
    version_check.record(latest, branch)
    if latest != installed:
        err_console.print("[muted]Update available! Run: omm update[/muted]")


_SKIP_ONBOARDING_SUBCOMMANDS = {"setup", "doctor", "help", "update", "_bg-version-check"}


def _ask_setup_choice() -> str:
    """The first-run setup gate itself, split out so tests can stub it
    without a real terminal (same shape as _ask_upload_choice). Before this
    existed, the only way out of a repeatedly auto-triggered wizard was to
    finish it or hand-edit config.json - Ctrl+C during the wizard
    deliberately leaves onboarding_completed False (see
    test_bare_omm_leaves_onboarding_incomplete_when_wizard_aborted) so it
    would just come back on the next command."""
    return _ask_single_key(
        "Run the first-time setup wizard now?",
        [("y", "Yes", "run"), ("n", "Not now", "later"), ("s", "Skip for good", "skip")],
        default_value="run",
        instruction="(y/n/s - s skips setup for good; `omm setup` reruns it any time)",
    )


def _maybe_run_onboarding(ctx: typer.Context) -> None:
    """Runs the first-time setup wizard exactly once, only for a genuinely
    fresh install (see config.load_config()'s migration handling) and only
    when there's a real terminal to drive questionary's checklist. Gates
    every subcommand, not just the bare `omm` invocation, so a first-time
    user running e.g. `omm contribute` directly still gets the wizard
    before their command executes."""
    if ctx.invoked_subcommand in _SKIP_ONBOARDING_SUBCOMMANDS:
        return
    if load_config().get("onboarding_completed", True):
        return
    if not _stdin_is_tty():
        return
    choice = _ask_setup_choice()
    if choice == "skip":
        config_mod.update_config(onboarding_completed=True)
        console.print(
            "[muted]Setup skipped. Run `omm setup` any time to configure omm.[/muted]"
        )
        return
    if choice == "later":
        return
    onboarding.run_wizard(console)
    config_mod.update_config(onboarding_completed=True)


def _maybe_start_update_check(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand in _SKIP_UPDATE_CHECK_SUBCOMMANDS:
        return
    installed = _installed_commit()
    if not installed:  # editable/dev install - nothing to compare against
        return
    branch = _channel_branch()
    fresh, latest = version_check.cached_remote_head_if_fresh(branch)
    if fresh:
        if latest and latest != installed:
            opts = ctx.ensure_object(GlobalOptions)

            def print_notice_unless_suppressed() -> None:
                if _update_notice_is_wanted(opts):
                    _confirm_and_print_update_notice(latest, installed, branch)

            ctx.call_on_close(print_notice_unless_suppressed)
        return
    if version_check.should_start_check(branch) and version_check.mark_checking(branch) is not False:
        args = [sys.executable, "-m", "omm.cli", "_bg-version-check"]
        # `start_new_session` (setsid) is POSIX-only - CPython's Windows
        # _execute_child ignores it entirely, so the child stays in the
        # parent's process group and can be torn down with it (e.g. the
        # console window closing) before the `git ls-remote` it runs
        # finishes. DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP is the
        # Windows equivalent of actually detaching it.
        if platform.system() == "Windows":
            kwargs = {
                "creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            }
        else:
            kwargs = {"start_new_session": True}
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
        except OSError:
            pass


_SKIP_AUTO_IMPORT_SUBCOMMANDS = {
    "update",
    "help",
    "import",
    "contribute",
    "doctor",
    "_bg-version-check",
}


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

    opts = _global_opts()
    found = scan_import.find_external_models(extra_path)
    groups = scan_import.group_by_hash(found)
    if not groups:
        if not opts.quiet:
            console.print("[muted]No externally-managed .gguf files found.[/muted]")
        return

    total_gb = sum(g.size_bytes for g in groups) / (1024**3)
    if not opts.quiet:
        console.print(
            f"Found {len(groups)} model(s) ({len(found)} file(s), ~{total_gb:.1f} GB) "
            "in supported local AI apps not yet managed by omm."
        )
    if not yes and not _ask_confirm(f"Import {len(groups)} model(s) into the omm hub?"):
        err_console.print("[warning]Skipped.[/warning]")
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
        err_console.print("[warning]Nothing selected, skipped.[/warning]")
        return

    bytes_saved = 0
    for group in groups:
        if group.sha256 not in selected_hashes:
            continue
        try:
            result = scan_import.adopt_group(group)
        except (OSError, linker.LinkError) as e:
            err_console.print(f"[warning]Could not import {group.display_name}: {e}[/warning]")
            continue
        bytes_saved += result.bytes_saved
        if not opts.quiet:
            console.print(f"  [success]Ω Imported {result.filename}[/success]")
        for warning in result.link_warnings:
            err_console.print(f"[warning]{warning}[/warning]")

    if bytes_saved > 0:
        config_mod.add_storage_saved_bytes(bytes_saved)

    final_count = len(registry.load_registry())
    console.print(
        f"[success]Done: {final_count} model(s) in the omm hub, "
        f"{bytes_saved / (1024**3):.1f} GB saved.[/success]"
    )


@app.command(name="import")
@global_flags
def import_cmd(
    path: str = typer.Argument(
        None, help="Optional extra directory to also scan for stray .gguf files."
    ),
) -> None:
    """Adopt .gguf files from other local AI apps into the omm hub.

    Scans every supported local AI app (and optionally PATH) for files not
    yet managed by omm, then offers to adopt each one it finds."""
    extra_path = None
    if path:
        extra_path = Path(path).expanduser()
        if not extra_path.is_dir():
            err_console.print(f"[error]Not a directory: {extra_path}[/error]")
            raise typer.Exit(1)
    _run_import_flow(extra_path, yes=_global_opts().yes)


# pipx gives no byte-level install progress, but it does print a fixed,
# ordered sequence of stage lines to stdout - use those as real (if coarse)
# progress checkpoints instead of an indeterminate animation that never
# actually reflects how far along the install is.
_PIPX_INSTALL_STAGES = [
    "creating virtual environment",
    "determining package name",
    "from spec",
    "done!",
    "installed package",
]
_PIPX_COMMAND = "pipx"
_PIPX_CURRENT_ENV = package_metadata.DISTRIBUTION_NAME
_PIPX_LEGACY_ENV = "omm"
_PIPX_EXPECTED_APPS = {"omm", "localfit-server"}


def _pipx_app_names(apps: object) -> list[str] | None:
    """pipx records app names as the launcher filenames, so on Windows the
    metadata says `omm.exe` / `localfit-server.exe` where POSIX says
    `omm` / `localfit-server`. Strip the suffix so every app-set check
    below compares the same names on both platforms. None if the
    metadata isn't a list of strings."""
    if not isinstance(apps, list) or not all(isinstance(app, str) for app in apps):
        return None
    return [app[:-4] if app.casefold().endswith(".exe") else app for app in apps]


@dataclass(frozen=True)
class _PipxInstallVerification:
    local_venvs: Path
    bin_dir: Path
    snapshot: dict
    internal_omm: Path
    exposed_omm: Path
    expected_version: str


@dataclass(frozen=True)
class _LegacyPipxState:
    local_venvs: Path
    bin_dir: Path
    snapshot: dict
    internal_omm: Path
    exposed_omm: Path
    owned_apps: tuple[tuple[Path, Path], ...] = ()
    expected_version: str = ""


def _run_pipx_query(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_pipx_child_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="command timed out")


def _pipx_snapshot_path(value: object) -> Path | None:
    if isinstance(value, str):
        return Path(value)
    if isinstance(value, dict) and value.get("__type__") == "Path":
        raw_path = value.get("__Path__")
        return Path(raw_path) if isinstance(raw_path, str) else None
    return None


def _pipx_owned_app_paths(apps: list[str], app_paths: list[Path]) -> dict[str, Path] | None:
    """Match pipx app paths by filename, never by list ordering."""

    matched: dict[str, Path] = {}
    for app in apps:
        expected_names = {app.casefold(), f"{app}.exe".casefold()}
        candidates = [path for path in app_paths if path.name.casefold() in expected_names]
        if len(candidates) != 1:
            return None
        matched[app] = candidates[0]
    return matched


def _pipx_app_exposure_matches(internal_app: Path, exposed_app: Path) -> bool:
    if not internal_app.is_file() or not exposed_app.is_file():
        return False
    if platform.system() == "Windows":
        try:
            return sha256_file(internal_app) == sha256_file(exposed_app)
        except OSError:
            return False
    try:
        return os.path.samefile(internal_app, exposed_app)
    except OSError:
        return False


def _pipx_environment_value(name: str) -> tuple[Path | None, str | None]:
    result = _run_pipx_query([_PIPX_COMMAND, "environment", "--value", name])
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or f"pipx returned no value for {name}"
        return None, detail
    return Path(result.stdout.strip()).expanduser(), None


def _pipx_metadata(snapshot: dict, environment: str) -> dict | None:
    venvs = snapshot.get("venvs")
    if not isinstance(venvs, dict):
        return None
    metadata = venvs.get(environment)
    if not isinstance(metadata, dict):
        return None
    if isinstance(metadata.get("metadata"), dict):
        metadata = metadata["metadata"]
    return metadata


def _pipx_snapshot() -> tuple[dict | None, str | None]:
    listed = _run_pipx_query([_PIPX_COMMAND, "list", "--json"])
    if listed.returncode != 0:
        return None, listed.stderr.strip() or "pipx list --json failed"
    try:
        snapshot = json.loads(listed.stdout)
    except (TypeError, ValueError):
        return None, "pipx list --json returned invalid JSON"
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("pipx_spec_version"), str)
        or not isinstance(snapshot.get("venvs"), dict)
    ):
        return None, "pipx list --json did not contain a versioned venvs map"
    return snapshot, None


def _verify_pipx_installation() -> tuple[_PipxInstallVerification | None, str | None]:
    """Verify pipx metadata, internal app, exposed app, and exact version."""

    local_venvs, error = _pipx_environment_value("PIPX_LOCAL_VENVS")
    if error:
        return None, error
    bin_dir, error = _pipx_environment_value("PIPX_BIN_DIR")
    if error:
        return None, error
    assert local_venvs is not None and bin_dir is not None

    snapshot, error = _pipx_snapshot()
    if error:
        return None, error
    assert snapshot is not None
    metadata = _pipx_metadata(snapshot, _PIPX_CURRENT_ENV)
    if metadata is None:
        return None, f"pipx environment {_PIPX_CURRENT_ENV!r} was not found"
    # pipx metadata 0.5 (used by pipx 1.11.1) has no `environment` field;
    # newer metadata may include it, in which case it must match exactly.
    if metadata.get("environment") not in (None, _PIPX_CURRENT_ENV):
        return None, "pipx environment metadata did not identify omm-model exactly"
    main_package = metadata.get("main_package")
    if not isinstance(main_package, dict):
        return None, "pipx omm-model environment has no main_package metadata"
    if main_package.get("package") != package_metadata.DISTRIBUTION_NAME:
        return None, "pipx omm-model environment contains the wrong main package"

    expected_version = _omm_version()
    if main_package.get("package_version") != expected_version:
        return None, "pipx omm-model environment contains the wrong package version"
    apps = _pipx_app_names(main_package.get("apps"))
    app_paths = main_package.get("app_paths")
    if apps is None or set(apps) != _PIPX_EXPECTED_APPS:
        return None, "pipx omm-model environment exposes an unexpected app set"
    if not isinstance(app_paths, list) or len(app_paths) != len(apps):
        return None, "pipx omm-model app paths do not match its app list"
    decoded_paths = [_pipx_snapshot_path(value) for value in app_paths]
    if any(path is None for path in decoded_paths):
        return None, "pipx omm-model app metadata contains an invalid path"
    valid_paths = [path for path in decoded_paths if path is not None]
    internal_by_name = _pipx_owned_app_paths(apps, valid_paths)
    if internal_by_name is None:
        return None, "pipx omm-model app paths do not identify each app exactly"
    expected_venv = (local_venvs / _PIPX_CURRENT_ENV).resolve()
    for app, internal_app in internal_by_name.items():
        try:
            internal_app.resolve().relative_to(expected_venv)
        except (OSError, ValueError):
            return None, f"pipx internal {app} executable is outside the omm-model environment"
        exposed_app = bin_dir / (
            f"{app}.exe" if platform.system() == "Windows" else app
        )
        if not _pipx_app_exposure_matches(internal_app, exposed_app):
            return None, f"the exposed {app} command does not match the omm-model executable"

    internal_omm = internal_by_name["omm"]
    exposed_omm = bin_dir / ("omm.exe" if platform.system() == "Windows" else "omm")

    expected_output = f"omm {expected_version}"
    for executable, label in ((internal_omm, "internal"), (exposed_omm, "exposed")):
        version_result = _run_pipx_query([str(executable), "--version"])
        if version_result.returncode != 0 or version_result.stdout.strip() != expected_output:
            return None, f"pipx {label} omm executable failed exact version verification"

    return (
        _PipxInstallVerification(
            local_venvs=local_venvs,
            bin_dir=bin_dir,
            snapshot=snapshot,
            internal_omm=internal_omm,
            exposed_omm=exposed_omm,
            expected_version=expected_version,
        ),
        None,
    )


def _capture_legacy_pipx_state() -> tuple[_LegacyPipxState | None, str | None]:
    local_venvs, error = _pipx_environment_value("PIPX_LOCAL_VENVS")
    if error:
        return None, error
    bin_dir, error = _pipx_environment_value("PIPX_BIN_DIR")
    if error:
        return None, error
    assert local_venvs is not None and bin_dir is not None
    expected_legacy_venv = (local_venvs / _PIPX_LEGACY_ENV).resolve()
    try:
        running_venv = Path(sys.prefix).resolve()
    except OSError:
        return None, "the running Python environment could not be resolved"
    if running_venv != expected_legacy_venv:
        return None, "the running process is not inside the legacy pipx omm environment"
    snapshot, error = _pipx_snapshot()
    if error:
        return None, error
    assert snapshot is not None
    metadata = _pipx_metadata(snapshot, _PIPX_LEGACY_ENV)
    if metadata is None or metadata.get("environment") not in (None, _PIPX_LEGACY_ENV):
        return None, "the running legacy pipx environment was not identified exactly"
    main_package = metadata.get("main_package")
    if not isinstance(main_package, dict) or main_package.get("package") != _PIPX_LEGACY_ENV:
        return None, "the legacy pipx environment contains the wrong main package"
    legacy_version = main_package.get("package_version")
    if not isinstance(legacy_version, str) or not legacy_version:
        return None, "the legacy pipx environment has no exact package version"
    apps = _pipx_app_names(main_package.get("apps"))
    app_paths = main_package.get("app_paths")
    if apps is None or set(apps) != _PIPX_EXPECTED_APPS:
        return None, "the legacy pipx environment exposes an unexpected app set"
    if not isinstance(app_paths, list) or len(app_paths) != len(apps):
        return None, "the legacy pipx environment has invalid app paths"
    decoded_paths = [_pipx_snapshot_path(value) for value in app_paths]
    if any(path is None for path in decoded_paths):
        return None, "the legacy pipx environment has an undecodable app path"
    valid_paths = [path for path in decoded_paths if path is not None]
    internal_by_name = _pipx_owned_app_paths(apps, valid_paths)
    if internal_by_name is None:
        return None, "the legacy pipx app paths do not identify each app exactly"
    for app, internal_app in internal_by_name.items():
        try:
            internal_app.resolve().relative_to(expected_legacy_venv)
        except (OSError, ValueError):
            return None, f"the legacy internal {app} executable is outside its environment"
    internal_omm = internal_by_name["omm"]
    executable_name = "omm.exe" if platform.system() == "Windows" else "omm"
    exposed_omm = bin_dir / executable_name
    owned_apps = tuple(
        (
            internal_by_name[app],
            bin_dir / (f"{app}.exe" if platform.system() == "Windows" else app),
        )
        for app in sorted(_PIPX_EXPECTED_APPS)
    )
    state = _LegacyPipxState(
        local_venvs=local_venvs,
        bin_dir=bin_dir,
        snapshot=snapshot,
        internal_omm=internal_omm,
        exposed_omm=exposed_omm,
        owned_apps=owned_apps,
        expected_version=legacy_version,
    )
    verified, error = _verify_legacy_pipx_execution(state)
    return (state, None) if verified else (None, error)


def _verify_legacy_pipx_execution(state: _LegacyPipxState) -> tuple[bool, str | None]:
    snapshot, error = _pipx_snapshot()
    if error:
        return False, error
    assert snapshot is not None
    metadata = _pipx_metadata(snapshot, _PIPX_LEGACY_ENV)
    if metadata is None or metadata.get("environment") not in (None, _PIPX_LEGACY_ENV):
        return False, "legacy pipx metadata is missing"
    main_package = metadata.get("main_package")
    if not isinstance(main_package, dict) or main_package.get("package") != _PIPX_LEGACY_ENV:
        return False, "legacy pipx main package identity changed"
    expected_version = state.expected_version or _omm_version()
    if main_package.get("package_version") != expected_version:
        return False, "legacy pipx package version changed"
    apps = _pipx_app_names(main_package.get("apps"))
    app_paths = main_package.get("app_paths")
    if apps is None or set(apps) != _PIPX_EXPECTED_APPS:
        return False, "legacy pipx app set changed"
    if not isinstance(app_paths, list) or len(app_paths) != len(apps):
        return False, "legacy pipx app path metadata changed"
    decoded_paths = [_pipx_snapshot_path(value) for value in app_paths]
    if any(path is None for path in decoded_paths):
        return False, "legacy pipx app path metadata became invalid"
    valid_paths = [path for path in decoded_paths if path is not None]
    current_internal_by_name = _pipx_owned_app_paths(apps, valid_paths)
    if current_internal_by_name is None:
        return False, "legacy pipx app paths no longer identify each app exactly"
    owned_apps = state.owned_apps or ((state.internal_omm, state.exposed_omm),)
    captured_internal_by_name = _pipx_owned_app_paths(
        sorted(_PIPX_EXPECTED_APPS), [internal for internal, _ in owned_apps]
    )
    if captured_internal_by_name is None:
        return False, "captured legacy app paths are ambiguous"
    expected_legacy_venv = (state.local_venvs / _PIPX_LEGACY_ENV).resolve()
    for app, current_internal in current_internal_by_name.items():
        captured_internal = captured_internal_by_name[app]
        try:
            current_internal.resolve().relative_to(expected_legacy_venv)
        except (OSError, ValueError):
            return False, f"legacy app {app} moved outside its environment"
        if current_internal.absolute() != captured_internal.absolute():
            return False, f"legacy app {app} internal path changed"
    for internal_app, exposed_app in owned_apps:
        if not _pipx_app_exposure_matches(internal_app, exposed_app):
            return False, f"legacy app {internal_app.name} has the wrong exposed identity"
    expected_output = f"omm {expected_version}"
    for executable in (state.internal_omm, state.exposed_omm):
        result = _run_pipx_query([str(executable), "--version"])
        if result.returncode != 0 or result.stdout.strip() != expected_output:
            return False, "legacy omm failed exact version verification"
    return True, None


def _restore_legacy_pipx_exposure(state: _LegacyPipxState) -> tuple[bool, str | None]:
    owned_apps = state.owned_apps or ((state.internal_omm, state.exposed_omm),)
    for internal_app, exposed_app in owned_apps:
        temporary = exposed_app.with_name(
            f".{exposed_app.name}.omm-rollback-{os.getpid()}"
        )
        try:
            temporary.unlink(missing_ok=True)
            if platform.system() == "Windows":
                shutil.copy2(internal_app, temporary)
            else:
                temporary.symlink_to(internal_app)
            os.replace(temporary, exposed_app)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            return False, f"{exposed_app}: {error}"
    return True, None


def _legacy_rollback_instructions(state: _LegacyPipxState) -> str:
    owned_apps = state.owned_apps or ((state.internal_omm, state.exposed_omm),)
    if platform.system() == "Windows":
        restore_commands = [
            f'copy /Y "{internal}" "{exposed}"' for internal, exposed in owned_apps
        ]
    else:
        restore_commands = [
            f'ln -sf "{internal}" "{exposed}"' for internal, exposed in owned_apps
        ]
    return "\n".join(["pipx uninstall omm-model", *restore_commands])


def _rollback_legacy_pipx_migration(
    state: _LegacyPipxState,
    reason: str,
) -> subprocess.CompletedProcess:
    """Best-effort rollback after any unverified new-environment result."""

    rollback_details: list[str] = []
    snapshot, snapshot_error = _pipx_snapshot()
    if snapshot is None:
        rollback_details.append(snapshot_error or "could not read pipx metadata")
    else:
        new_metadata = _pipx_metadata(snapshot, _PIPX_CURRENT_ENV)
        if new_metadata is not None:
            main_package = new_metadata.get("main_package")
            safe_new_environment = (
                new_metadata.get("environment") in (None, _PIPX_CURRENT_ENV)
                and isinstance(main_package, dict)
                and main_package.get("package") == package_metadata.DISTRIBUTION_NAME
            )
            if safe_new_environment:
                uninstall = _run_pipx_query(
                    [_PIPX_COMMAND, "uninstall", _PIPX_CURRENT_ENV]
                )
                if uninstall.returncode != 0:
                    rollback_details.append(
                        uninstall.stderr.strip() or "pipx uninstall omm-model failed"
                    )
            else:
                rollback_details.append("refused to remove an unverified omm-model environment")

    restored, restore_error = _restore_legacy_pipx_exposure(state)
    if not restored:
        rollback_details.append(restore_error or "legacy exposure restore failed")
    legacy_ok, legacy_error = _verify_legacy_pipx_execution(state) if restored else (False, None)
    if not legacy_ok:
        rollback_details.append(legacy_error or "legacy execution verification failed")

    instructions = _legacy_rollback_instructions(state)
    if legacy_ok:
        detail = "; ".join(rollback_details)
        suffix = f" Remaining cleanup: {detail}." if detail else ""
        message = (
            f"{reason}\nThe legacy omm command was restored and verified.{suffix}\n"
            f"Retry later. If needed, run:\n{instructions}"
        )
    else:
        detail = "; ".join(rollback_details) or "unknown rollback failure"
        message = (
            f"{reason}\nAutomatic rollback could not be verified ({detail}). Run:\n"
            f"{instructions}"
        )
    return subprocess.CompletedProcess([], 1, stdout="", stderr=message)


def _legacy_pipx_environment_is_current(verification: _PipxInstallVerification) -> bool:
    metadata = _pipx_metadata(verification.snapshot, _PIPX_LEGACY_ENV)
    if metadata is None:
        return False
    main_package = metadata.get("main_package")
    apps = _pipx_app_names(main_package.get("apps")) if isinstance(main_package, dict) else None
    if (
        metadata.get("environment") not in (None, _PIPX_LEGACY_ENV)
        or not isinstance(main_package, dict)
        or main_package.get("package") != _PIPX_LEGACY_ENV
        or apps is None
        or "omm" not in apps
    ):
        return False
    try:
        return Path(sys.prefix).resolve() == (
            verification.local_venvs / _PIPX_LEGACY_ENV
        ).resolve()
    except OSError:
        return False


def _warn_legacy_pipx_cleanup(detail: str) -> None:
    err_console.print(
        "[warning]The new omm-model pipx environment is verified, but the legacy "
        f"omm environment was kept ({escape(detail)}).[/warning]\n"
        "Close other OMM terminals, then run: pipx uninstall omm"
    )


def _cleanup_legacy_pipx_environment(
    verification: _PipxInstallVerification,
) -> subprocess.CompletedProcess:
    """Remove only the verified currently-running legacy environment.

    Cleanup failure is non-fatal because the new environment and exposed app
    were already verified. On Windows in particular, the current legacy venv
    may remain locked until this process exits.
    """

    if not _legacy_pipx_environment_is_current(verification):
        _warn_legacy_pipx_cleanup("it could not be identified safely")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    uninstall = _run_pipx_query([_PIPX_COMMAND, "uninstall", _PIPX_LEGACY_ENV])
    if uninstall.returncode != 0:
        ensured = _ensure_new_pipx_installation_after_cleanup(legacy_removed=False)
        if ensured.returncode != 0:
            return ensured
        _warn_legacy_pipx_cleanup(uninstall.stderr.strip() or "pipx uninstall failed")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    # pipx 1.11.1 has no `expose` command. First check whether uninstall
    # preserved the new app link; only reinstall the already-verified editable
    # spec when repair is actually needed.
    return _ensure_new_pipx_installation_after_cleanup(legacy_removed=True)


def _ensure_new_pipx_installation_after_cleanup(
    *, legacy_removed: bool,
) -> subprocess.CompletedProcess:
    reverified, error = _verify_pipx_installation()
    if reverified is not None:
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    repair = _run_pipx_install_with_progress(
        [_PIPX_COMMAND, "install", "--force", "--editable", _install_spec()]
    )
    if repair.returncode != 0:
        context = (
            "Legacy pipx environment was removed"
            if legacy_removed
            else "Legacy pipx uninstall failed and may have changed shared app links"
        )
        return subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr=(
                f"{context}, and the omm-model app link repair "
                f"failed after verification reported {error}. Run: pipx install --force "
                f"--editable {_install_spec()}\n{repair.stderr}"
            ),
        )
    reverified, error = _verify_pipx_installation()
    if reverified is None:
        context = (
            "Legacy pipx environment was removed"
            if legacy_removed
            else "Legacy pipx uninstall failed and may have changed shared app links"
        )
        return subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr=(
                f"{context}, and omm-model failed final "
                f"verification ({error}). Run: pipx install --force --editable "
                f"{_install_spec()}"
            ),
        )
    return subprocess.CompletedProcess([], 0, stdout="", stderr="")


def _finalize_legacy_pipx_migration(
    install_result: subprocess.CompletedProcess,
    legacy_state: _LegacyPipxState,
) -> subprocess.CompletedProcess:
    if install_result.returncode != 0:
        reason = install_result.stderr.strip() or "pipx install failed"
        return _rollback_legacy_pipx_migration(legacy_state, reason)
    verification, error = _verify_pipx_installation()
    if verification is None:
        return _rollback_legacy_pipx_migration(
            legacy_state,
            "pipx reported a successful legacy migration, but the new omm-model "
            f"environment failed verification ({error}).",
        )
    return _cleanup_legacy_pipx_environment(verification)


def _pipx_child_env() -> dict[str, str]:
    """Environment for a piped `pipx` child that makes its output UTF-8.

    pipx is itself a Python program, so with stdout redirected it encodes
    using the locale code page (cp949 on Korean Windows) unless told
    otherwise. Setting PYTHONIOENCODING makes the child emit the UTF-8 that
    `_run_pipx_install` decodes, instead of leaving the two ends to disagree
    the moment a package name or install path contains a non-ASCII
    character (a Korean Windows user profile directory is enough)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # `python -m pipx ensurepath` updates future login shells, not the shell
    # already running the installer. On macOS a user install commonly lands
    # in ~/Library/Python/X.Y/bin, so the first `omm update` must make that
    # interpreter's user scripts directory visible to its pipx child itself.
    user_scheme = "nt_user" if os.name == "nt" else (
        "osx_framework_user" if sys.platform == "darwin" else "posix_user"
    )
    try:
        user_scripts = sysconfig.get_path("scripts", scheme=user_scheme)
    except (KeyError, TypeError, ValueError):
        user_scripts = None
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    fallback_dirs = [Path.home() / ".local" / "bin"]
    if user_scripts:
        fallback_dirs.insert(0, Path(user_scripts))
    normalized = {os.path.normcase(os.path.abspath(entry)) for entry in path_entries}
    for directory in fallback_dirs:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in normalized:
            path_entries.append(str(directory))
            normalized.add(key)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def _run_pipx_install(args: list[str], progress: Progress, task_id) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_pipx_child_env(),
    )
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


def _declared_dependencies() -> list[str] | None:
    """Dependency specifications from the freshly-pulled ``pyproject.toml``.

    Parse TOML instead of scraping the first bracketed ``dependencies`` text:
    comments, single-quoted values, or another table containing the same word
    must not make the updater inspect the wrong package set.
    """
    try:
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib
        with (SRC_DIR / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, TypeError, ValueError):
        return None
    project = document.get("project") if isinstance(document, dict) else None
    dependencies = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(dependencies, list) or not all(
        isinstance(spec, str) and spec.strip() for spec in dependencies
    ):
        return None
    return [spec.strip() for spec in dependencies]


def _dependency_spec_satisfied(spec: str, installed_version: str) -> bool:
    """Conservatively evaluate the dependency forms used by this project.

    OMM intentionally does not add a runtime dependency merely to decide
    whether its updater needs to reinstall runtime dependencies.  The current
    project uses dotted numeric lower bounds and one ``python_version`` marker;
    any future/unknown PEP 508 form returns ``False`` and triggers a safe pipx
    refresh instead of incorrectly declaring an old environment current.
    """
    requirement, _, marker = spec.partition(";")
    marker = marker.strip()
    if marker:
        marker_match = re.fullmatch(
            r"python_version\s*(<=|>=|==|!=|<|>)\s*['\"](\d+(?:\.\d+)*)['\"]",
            marker,
        )
        if marker_match is None:
            return False
        current = ".".join(str(value) for value in sys.version_info[:2])
        applies = _compare_dotted_versions(current, marker_match.group(1), marker_match.group(2))
        if applies is None:
            return False
        if not applies:
            return True

    match = re.fullmatch(
        r"\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(.*)\s*", requirement
    )
    if match is None:
        return False
    constraints = match.group(2).strip()
    if not constraints:
        return True
    for constraint in constraints.split(","):
        constraint_match = re.fullmatch(
            r"\s*(<=|>=|==|!=|<|>)\s*(\d+(?:\.\d+)*)\s*", constraint
        )
        if constraint_match is None:
            return False
        satisfied = _compare_dotted_versions(
            installed_version, constraint_match.group(1), constraint_match.group(2)
        )
        if satisfied is not True:
            return False
    return True


def _compare_dotted_versions(installed: str, operator: str, required: str) -> bool | None:
    def parts(value: str) -> tuple[int, ...] | None:
        if re.fullmatch(r"\d+(?:\.\d+)*", value.strip()) is None:
            return None
        return tuple(int(part) for part in value.strip().split("."))

    left, right = parts(installed), parts(required)
    if left is None or right is None:
        return None
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return {
        "<": left < right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
        ">=": left >= right,
        ">": left > right,
    }.get(operator)


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

    dependencies = _declared_dependencies()
    if dependencies is None:
        return False
    for spec in dependencies:
        name_match = re.match(r"\s*([A-Za-z0-9_.-]+)", spec)
        if name_match is None:
            return False
        name = name_match.group(1)
        try:
            installed_version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return False
        if not _dependency_spec_satisfied(spec, installed_version):
            return False
    return True


def _run_pipx_install_with_progress(args: list[str]) -> subprocess.CompletedProcess:
    with Progress(
        SpinnerColumn(),
        TextColumn("[accent]Reinstalling omm via pipx...[/accent]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        disable=_global_opts().quiet,
    ) as progress:
        task_id = progress.add_task("upgrade", total=len(_PIPX_INSTALL_STAGES))
        result = _run_pipx_install(args, progress, task_id)
        progress.update(task_id, completed=len(_PIPX_INSTALL_STAGES))
    return result


def _verified_pipx_install_result(
    result: subprocess.CompletedProcess,
) -> subprocess.CompletedProcess:
    """Convert a false-successful pipx result into an updater failure."""
    if result.returncode != 0:
        return result
    verification, error = _verify_pipx_installation()
    if verification is not None:
        return result
    detail = error or "unknown pipx verification failure"
    return subprocess.CompletedProcess(
        result.args,
        1,
        stdout=result.stdout,
        stderr=(
            "pipx reported a successful install, but the new omm-model "
            f"environment failed exact verification ({detail})."
        ),
    )


def _remove_update_path(path: Path) -> None:
    """Remove only one updater-owned scratch/backup path."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _migrate_to_editable_install(branch: str = "main") -> subprocess.CompletedProcess:
    """First-run (or self-heal) path: clone the repo into a scratch dir,
    swap it into place as SRC_DIR only once the clone has actually
    succeeded, then pipx --editable-install it, so future `omm update`
    calls are a `git pull` instead of a full pipx reinstall. Runs whenever
    SRC_DIR isn't a valid git checkout - regardless of whether the
    currently installed commit already matches latest, since the goal is
    switching mechanism, not code.

    `branch` picks the update channel (main=stable, beta=beta); see
    `omm setting version`.

    Clones into SRC_DIR.new rather than SRC_DIR directly: a clone that
    fails partway (network drop, timeout, Ctrl-C) must not destroy a
    working editable install - previously an rmtree-then-clone order left
    `omm` permanently broken with ModuleNotFoundError until reinstalled
    from scratch."""
    console.print("[accent]Migrating to fast-update mode (one-time)...[/accent]")
    tmp_dir = SRC_DIR.with_name(SRC_DIR.name + ".new")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        clone = subprocess.run(
            [
                "git", "clone", "--filter=blob:none", "--branch", branch,
                "--single-branch", "--quiet", _BARE_REPO_URL, str(tmp_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return subprocess.CompletedProcess([], 1, stdout="", stderr="git clone timed out")
    if clone.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return clone

    head = subprocess.run(
        ["git", "-C", str(tmp_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if head.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return head
    # Verified against the *currently running* omm's own bundled anchor
    # (the old, already-vetted install) - not tmp_dir's own copy, which an
    # attacker with push access could have edited in the same commit.
    verified_commit, message = trust.verified_install_commit(
        tmp_dir, head.stdout.strip(), trust.current_trust_anchor()
    )
    if verified_commit is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return subprocess.CompletedProcess([], 1, stdout="", stderr=message)
    if verified_commit != head.stdout.strip():
        checkout = subprocess.run(
            ["git", "-C", str(tmp_dir), "checkout", "--detach", "--quiet", verified_commit],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        if checkout.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return checkout

    backup_dir = SRC_DIR.with_name(
        f"{SRC_DIR.name}.previous-{os.getpid()}-{time.time_ns()}"
    )
    had_existing_src = SRC_DIR.exists() or SRC_DIR.is_symlink()
    try:
        if had_existing_src:
            SRC_DIR.rename(backup_dir)
        tmp_dir.rename(SRC_DIR)
    except OSError:
        if had_existing_src and backup_dir.exists() and not SRC_DIR.exists():
            backup_dir.rename(SRC_DIR)
        raise

    install_succeeded = False
    try:
        result = _verified_pipx_install_result(
            _run_pipx_install_with_progress(
                [_PIPX_COMMAND, "install", "--force", "--editable", _install_spec()]
            )
        )
        install_succeeded = result.returncode == 0
        return result
    finally:
        if install_succeeded:
            _remove_update_path(backup_dir)
        else:
            _remove_update_path(SRC_DIR)
            if had_existing_src and backup_dir.exists():
                backup_dir.rename(SRC_DIR)


def _run_git(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="git command timed out")


def _git_update_src(branch: str = "main") -> subprocess.CompletedProcess:
    """Fast path for an already-migrated install: fetch + fast-forward the
    persistent clone in place. The editable install's .pth points straight
    at SRC_DIR/src, so this alone is enough to pick up code changes - no
    pipx call needed unless dependencies themselves changed (checked by
    the caller via _deps_satisfied()).

    `branch` is the update channel's branch (main=stable, beta=beta); see
    `omm setting version`. Uses `checkout -B` rather than `reset --hard` so
    this also handles a channel switch, where SRC_DIR's currently checked
    out local branch may differ from `branch`. Fetches with an explicit
    refspec (not just `fetch origin <branch>`) because SRC_DIR was cloned
    `--single-branch`, which only auto-updates the remote-tracking ref for
    the branch it was cloned with - a bare branch name on the command line
    for any other branch would land in FETCH_HEAD only, leaving
    `origin/<branch>` missing for the rev-parse below.

    Verifies the target commit's signature against the anchor still on disk
    from *before* this fetch (SRC_DIR's own trust/allowed_signers, read
    prior to checkout overwriting it) before ever checking it out.

    Self-heals origin's URL to the current REPO_URL first: a clone made
    before a repo rename/transfer still has the old URL in its git config,
    and while GitHub redirects git operations for renamed/transferred repos,
    that redirect isn't guaranteed to last forever."""
    remote_url = _run_git(["git", "-C", str(SRC_DIR), "remote", "get-url", "origin"], timeout=10)
    if remote_url.returncode == 0 and remote_url.stdout.strip() != _BARE_REPO_URL:
        _run_git(["git", "-C", str(SRC_DIR), "remote", "set-url", "origin", _BARE_REPO_URL], timeout=10)

    current = _run_git(["git", "-C", str(SRC_DIR), "rev-parse", "HEAD"], timeout=10)
    current_commit = current.stdout.strip() if current.returncode == 0 else None

    fetch = _run_git(
        ["git", "-C", str(SRC_DIR), "fetch", "--quiet", "origin", f"{branch}:refs/remotes/origin/{branch}"]
    )
    if fetch.returncode != 0:
        return fetch

    rev_parse = _run_git(["git", "-C", str(SRC_DIR), "rev-parse", f"origin/{branch}"], timeout=10)
    if rev_parse.returncode != 0:
        return rev_parse
    target_commit = rev_parse.stdout.strip()

    ok, message = trust.verify_update(
        SRC_DIR, current_commit, target_commit, trust.current_trust_anchor()
    )
    if not ok:
        return subprocess.CompletedProcess([], 1, stdout="", stderr=message)

    return _run_git(
        ["git", "-C", str(SRC_DIR), "checkout", "-B", branch, f"origin/{branch}", "--force", "--quiet"]
    )


def _perform_update(branch: str) -> subprocess.CompletedProcess:
    """Shared by `omm update` and `omm setting version` (channel switch):
    migrate-or-pull SRC_DIR onto `branch`, reinstalling via pipx only if
    dependencies changed or the editable environment still carries OMM's
    validated legacy distribution name."""
    source = package_metadata.install_source()
    if source is not package_metadata.InstallSource.GIT:
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr=_package_managed_update_guidance(source)
        )

    found = package_metadata.find_distribution()
    legacy_distribution = found is not None and found[0] != package_metadata.DISTRIBUTION_NAME
    legacy_state = None
    if legacy_distribution:
        legacy_state, error = _capture_legacy_pipx_state()
        if legacy_state is None:
            return subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr=(
                    "Refusing to migrate the legacy pipx environment because its rollback "
                    f"state could not be verified ({error})."
                ),
            )

    migrated = _src_head_commit() is not None
    editable_install = _editable_install_uses_src()
    try:
        if not migrated:
            result = _migrate_to_editable_install(branch)
            return (
                _finalize_legacy_pipx_migration(result, legacy_state)
                if legacy_state is not None
                else result
            )
        console.print(f"Updating omm from {REPO_URL} ({branch}) ...")
        result = _git_update_src(branch)
        if result.returncode == 0:
            dependencies_satisfied = _deps_satisfied()
            if legacy_distribution or not dependencies_satisfied or not editable_install:
                # With the renamed distribution, pipx derives a new
                # `omm-model` environment from this spec. `--force` lets the
                # successfully-created environment take over the shared
                # `omm` app link; if installation fails, the non-zero result
                # is propagated and data refresh is skipped by the caller.
                result = _run_pipx_install_with_progress(
                    [_PIPX_COMMAND, "install", "--force", "--editable", _install_spec()]
                )
                result = _verified_pipx_install_result(result)
                if legacy_state is not None:
                    result = _finalize_legacy_pipx_migration(result, legacy_state)
        return result
    except FileNotFoundError:
        if platform.system() == "Windows":
            err_console.print(
                "[error]git or pipx not found. Install them first, or rerun the installer:[/error]\n"
                "  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                "irm https://omm.run/install.ps1 | iex"
            )
        else:
            err_console.print(
                "[error]git or pipx not found. Install them first, or rerun the installer:[/error]\n"
                "  curl -fsSL https://omm.run/install.sh | sh"
            )
        raise typer.Exit(1)
    except OSError as e:
        err_console.print(f"[error]Update failed: {e}[/error]")
        raise typer.Exit(1) from e


def _package_managed_update_guidance(
    source: package_metadata.InstallSource,
    attempted_command: str = "omm update",
) -> str:
    """Explain how to update without changing the installation mechanism."""

    instructions = {
        package_metadata.InstallSource.PIPX: (
            "This OMM installation is managed by pipx.\n"
            "Update it with: pipx upgrade omm-model"
        ),
        package_metadata.InstallSource.PYPI: (
            "This OMM installation is managed as a Python package.\n"
            "Update it with: python -m pip install --upgrade omm-model"
        ),
        package_metadata.InstallSource.HOMEBREW: (
            "This OMM installation is managed by Homebrew.\n"
            "Update it with: brew upgrade omm-hippo/omm/omm\n"
            "If the tap is already installed, `brew upgrade omm` also works."
        ),
        package_metadata.InstallSource.WINGET: (
            "This OMM installation is managed by winget.\n"
            "Update it with: winget upgrade --id OmmHippo.OMM -e"
        ),
        package_metadata.InstallSource.NPM: (
            "This OMM installation is managed by npm.\n"
            "Update it with: npm update --global @omm-hippo/omm"
        ),
        package_metadata.InstallSource.UNKNOWN: (
            "This OMM installation is not a canonical OMM Git source checkout.\n"
            "Update it with the same package manager that installed it."
        ),
    }
    detail = instructions.get(source, instructions[package_metadata.InstallSource.UNKNOWN])
    return (
        f"{detail}\n"
        f"`{attempted_command}` left the installation unchanged instead of replacing it "
        "with an editable Git checkout."
    )


# What to do next for each transient benchmark failure. A bare reason code
# ("model_load_failed") told a first-time tester nothing - seen in the
# 2026-08-23 promo dry run, where the fix was simply re-linking the model
# into Ollama.
_TRANSIENT_FAILURE_HINTS: dict[str, str] = {
    quality_mod.FAILURE_REASON_MODEL_LOAD_FAILED: (
        "The runtime could not find or load this model. Run `omm doctor`, then "
        "`omm link --engine ollama` (or `omm info <model>` to see which runners it is linked into) and retry."
    ),
    quality_mod.FAILURE_REASON_OLLAMA_UNAVAILABLE: (
        "Ollama is installed but not reachable. Start it (or run `omm doctor`) and retry."
    ),
    quality_mod.FAILURE_REASON_CONNECTION_ERROR: (
        "Could not connect to the runtime's local API. Start it (or run `omm doctor`) and retry."
    ),
    quality_mod.FAILURE_REASON_GENERATION_TIMEOUT: (
        "One generation ran past the time limit - often the first load after a cold start. Retry once; "
        "if it happens twice, omm records it as performance_unfit."
    ),
    quality_mod.FAILURE_REASON_NO_TIMING_METRICS: (
        "The runtime answered without timing data, so no speed could be measured. Retry; if it persists, "
        "update the runtime."
    ),
}


@app.command()
@global_flags
def doctor() -> None:
    """Diagnose the OMM install and Ollama links without changing state.

    WARN findings keep exit code 0; definite FAIL findings exit 1.
    """
    report = doctor_mod.collect_report(
        module_path=Path(__file__).resolve(),
        command_path=doctor_mod.running_command_path(),
    )
    if _global_opts().json:
        console.print_json(data=report.as_dict())
    else:
        table = Table(title="omm doctor")
        table.add_column("Status", no_wrap=True)
        table.add_column("Check", style="accent", no_wrap=True)
        table.add_column("Detail")
        status_styles = {"PASS": "success", "WARN": "warning", "FAIL": "error"}
        for check in report.checks:
            style = status_styles[check.status]
            table.add_row(
                f"[{style}]{check.status}[/{style}]",
                escape(check.name),
                escape(check.detail),
            )
        console.print(table)
        counts = {
            status: sum(check.status == status for check in report.checks)
            for status in ("PASS", "WARN", "FAIL")
        }
        overall_style = status_styles[report.status]
        console.print(
            f"[{overall_style}]Overall: {report.status}[/{overall_style}] "
            f"({counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail)"
        )
    if report.status == "FAIL":
        raise typer.Exit(1)


@app.command()
@global_flags
def update() -> None:
    """Reinstall omm from the latest source and refresh its data.

    Uses a persistent editable clone (SRC_DIR) for a git-pull-speed update
    once migrated; a one-time pipx --editable install otherwise. Pulls
    from whichever branch `omm setting version` has selected (stable/main
    by default, or beta)."""
    branch = _channel_branch()
    migrated = _src_head_commit() is not None
    editable_install = _editable_install_uses_src()
    installed = _installed_commit()
    latest = _remote_head_commit(branch) if installed else None
    if latest:
        version_check.record(latest, branch)
    if migrated and editable_install and installed and latest and installed == latest:
        console.print(f"[muted]omm is already up to date - {_version_line(installed)}[/muted]")
        _refresh_data()
        return

    before = _version_line(installed)
    result = _perform_update(branch)
    if result.returncode != 0:
        err_console.print(f"[error]Update failed:[/error]\n{result.stderr}")
        raise typer.Exit(1)

    after = _version_line(_installed_commit())
    console.print(f"[success]Ω Updated: {before} -> {after}[/success]")
    _refresh_data()


def _add_escape_to_cancel(question: questionary.Question) -> questionary.Question:
    """questionary only aborts on Ctrl+C/Ctrl+Q by default; make Escape do
    the same so `.ask()` returns None instead of requiring Ctrl+C.

    `application.key_bindings` is a `_MergedKeyBindings` (prompt_toolkit
    merges questionary's bindings with the default ones), which has no
    `.add` - it must be extended via `merge_key_bindings`, not mutated."""
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.keys import Keys

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    application = getattr(question, "application", None)
    key_bindings = getattr(application, "key_bindings", None)
    if key_bindings is not None:
        escape_binding = KeyBindings()
        escape_binding.add(Keys.Escape, eager=True)(_abort)
        application.key_bindings = merge_key_bindings([key_bindings, escape_binding])
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
            f"[error]{what} requires an interactive terminal. "
            "Re-run from a real terminal, or pass the flag that bypasses this prompt.[/error]"
        )
        raise typer.Exit(1)


def _ask_select(question: questionary.Question):
    _require_tty("This selection")
    return _add_escape_to_cancel(question).ask()


# Reverse of the standard 2-beolsik (두벌식) layout: what each jamo types as
# on a physical QWERTY key. A single-key prompt (y/n/a/...) should accept
# the jamo too, since pressing the key is what matters, not whether 한/영
# happens to be toggled on at the time.
_HANGUL_JAMO_TO_LATIN = {
    "ㅂ": "q", "ㅈ": "w", "ㄷ": "e", "ㄱ": "r", "ㅅ": "t",
    "ㅛ": "y", "ㅕ": "u", "ㅑ": "i", "ㅐ": "o", "ㅔ": "p",
    "ㅁ": "a", "ㄴ": "s", "ㅇ": "d", "ㄹ": "f", "ㅎ": "g",
    "ㅗ": "h", "ㅓ": "j", "ㅏ": "k", "ㅣ": "l",
    "ㅋ": "z", "ㅌ": "x", "ㅊ": "c", "ㅍ": "v", "ㅠ": "b",
    "ㅜ": "n", "ㅡ": "m",
}


def _build_single_key_bindings(
    choices: list[tuple[str, str, object]],
    default_value: object,
    status: dict[str, object],
):
    """KeyBindings for _ask_single_key, split out so tests can drive
    handlers directly with a fake event instead of running a real terminal
    app (see test_cli_recommend_escape.py for the same pattern applied to
    _add_escape_to_cancel). Each choice's key also responds to the jamo a
    Korean IME produces on the same physical key (_HANGUL_JAMO_TO_LATIN),
    so 한/영 toggle state doesn't matter. Written from scratch rather than
    extending questionary.confirm because its key bindings are already
    merged into the Question by the time we get it back, so extra bindings
    can't be bolted on afterwards."""
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    by_key: dict[str, tuple[str, object]] = {}
    for key, label, value in choices:
        by_key[key.lower()] = (label, value)
        by_key[key.upper()] = (label, value)

    bindings = KeyBindings()

    @bindings.add(Keys.Escape, eager=True)
    @bindings.add(Keys.ControlQ, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _abort(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    def _accept(label: str, value: object):
        def handler(event):
            status["answer"] = (label, value)
            event.app.exit(result=value)

        return handler

    for key, (label, value) in by_key.items():
        bindings.add(key)(_accept(label, value))
    for jamo, latin in _HANGUL_JAMO_TO_LATIN.items():
        if latin in by_key:
            label, value = by_key[latin]
            bindings.add(jamo)(_accept(label, value))

    @bindings.add(Keys.ControlM, eager=True)
    def _enter(event):
        event.app.exit(result=default_value)

    @bindings.add(Keys.Any)
    def _other(event):
        """Disallow inserting other text."""

    return bindings


def _ask_single_key(
    message: str,
    choices: list[tuple[str, str, object]],
    default_value: object,
    instruction: str,
) -> object:
    """Single-keypress prompt: each choice is (key, label, value); answers
    immediately on keypress (no Enter needed), like questionary.confirm but
    with an arbitrary key->value map instead of a hardcoded y/n."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import to_formatted_text
    from questionary.styles import merge_styles_default

    _require_tty(message)
    status: dict[str, object] = {"answer": None}
    bindings = _build_single_key_bindings(choices, default_value, status)

    def get_prompt_tokens():
        tokens = [("class:qmark", "?"), ("class:question", f" {message} ")]
        if status["answer"] is None:
            tokens.append(("class:instruction", f"{instruction} "))
        else:
            tokens.append(("class:answer", status["answer"][0]))
        return to_formatted_text(tokens)

    merged_style = merge_styles_default([None])
    return PromptSession(
        get_prompt_tokens, key_bindings=bindings, style=merged_style
    ).app.run()


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/no prompt that answers on the y/n keypress itself (no Enter
    needed)."""
    return bool(
        _ask_single_key(
            message,
            [("y", "Yes", True), ("n", "No", False)],
            default_value=default,
            instruction="(y/n)",
        )
    )


def _ask_upload_choice(prompt: str) -> str:
    """The telemetry-upload confirm, split out from _resolve_upload_decision
    so tests can stub it without going through a real terminal prompt."""
    return _ask_single_key(
        prompt,
        [("y", "Yes", "yes"), ("n", "No", "no"), ("a", "Always", "always")],
        default_value="no",
        instruction="(y/n/a - a saves 'always' as the new default)",
    )


def _print_not_installed_error(name: str) -> None:
    """`name` isn't a registry entry - shared by every command that looks
    one up (info/verify/run/upgrade/remove/link) so the fix text isn't
    duplicated per command."""
    errors.print_cli_error(
        err_console,
        f"{name} is not installed via omm.",
        fix="Run `omm list` to see what is installed.",
    )


def _print_no_engine_error(action: str) -> None:
    """Neither Ollama nor LM Studio is installed/reachable - shared by
    every `_select_benchmark_engine() is None` check (benchmark,
    contribute) so the fix text isn't duplicated per command."""
    errors.print_cli_error(
        err_console,
        "Neither Ollama nor LM Studio is installed or available.",
        fix=f"Install one of them, start it once, then retry `omm {action}`.",
    )


def _ensure_ollama_running(action: str, *, assume_yes: bool = False):
    """Preflight Ollama without confusing missing, stopped, and stale PATH."""
    state = benchmark.ollama_install_state()
    if state in {"running", "running_path_stale"}:
        if state == "running_path_stale" and not (
            _global_opts().quiet or _global_opts().json
        ):
            console.print(
                "[muted]Ollama API is running; the current terminal PATH has not "
                "picked up the Ollama command yet.[/muted]"
            )
        return None
    if state == "missing":
        errors.print_cli_error(
            err_console,
            "Ollama is not installed or its executable cannot be found.",
            fix=f"Install Ollama from https://ollama.com/download, start it once, then retry `omm {action}`.",
        )
        raise typer.Exit(1)

    prompt = f"Ollama is installed but stopped. Start it now for `omm {action}`?"
    if not assume_yes and (not _stdin_is_tty() or not _ask_confirm(prompt)):
        err_console.print(
            f"[error]omm {action} requires the Ollama API at {benchmark.OLLAMA_HOST}.[/error]"
        )
        raise typer.Exit(1)
    started = benchmark.start_ollama_daemon()
    if started is None:
        detail = benchmark.last_daemon_start_error() or "unknown startup failure"
        err_console.print(f"[error]Could not start Ollama: {detail}[/error]")
        raise typer.Exit(1)
    return started


def _resolve_upload_decision(prompt: str) -> bool:
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "always":
        return True
    if policy == "never":
        return False
    answer = _ask_upload_choice(prompt)
    if answer == "always":
        if load_config().get("telemetry_endpoint"):
            config_mod.update_config(telemetry_send_policy="always")
            console.print(
                "[muted]Saved: omm will now always send benchmark results "
                "(change with `omm setting upload`).[/muted]"
            )
        return True
    return answer == "yes"


def _resolve_error_report_decision(flag: bool) -> bool:
    """Decide once, before the unattended loop starts, whether this run may
    collect and send error reports.

    Same shape as the telemetry upload policy for the same reason: an
    unattended `omm contribute` must never be interrupted by a question
    mid-loop, so the single question is asked here or not at all. The
    differences from telemetry are the opt-in default and the fact that a
    stored `never` outranks the runtime flag.
    """
    config_data = load_config()
    policy = error_report.send_policy(config_data)
    explicitly_set = error_report.policy_is_set(config_data)
    if not error_report.enabled(config_data):
        if flag:
            err_console.print(
                "[warning]--report-errors needs a telemetry endpoint (the error-report "
                "channel is derived from it). Set one with `omm setting telemetry "
                "--endpoint` first.[/warning]"
            )
        return False
    if policy == "always":
        # Already opted in permanently; the flag has nothing left to add.
        return True
    if policy == "never":
        if explicitly_set:
            if flag:
                err_console.print(
                    "[warning]Error reports are turned off, so --report-errors is ignored. "
                    "Run `omm setting error-reports --ask` (or --enable) first if you want "
                    "to send them.[/warning]"
                )
            return False
        if flag:
            console.print(
                "[muted]--report-errors: error reports from this run only will be sent. "
                "Your saved setting is unchanged (`omm setting error-reports --enable` "
                "makes it permanent).[/muted]"
            )
            return True
        return False
    # policy == "ask"
    if flag:
        return True
    if not _stdin_is_tty() or _global_opts().yes:
        # No usable prompt, and consent is never assumed for this feature.
        return False
    report, is_example = error_report.preview_report()
    console.print(
        "[muted]omm would send "
        f"{'a report shaped exactly like this' if is_example else 'this report, already queued from an earlier run,'}"
        " to the write-only error-report channel (see docs/error-reports.md):[/muted]"
    )
    console.print(f"[muted]{escape(error_report.preview_text(report))}[/muted]", highlight=False)
    answer = _ask_upload_choice("Send scrubbed error reports?")
    if answer == "always":
        config_mod.update_config(error_report_send_policy="always")
        console.print(
            "[muted]Saved: omm will now always send scrubbed error reports "
            "(change with `omm setting error-reports`).[/muted]"
        )
        return True
    return answer == "yes"


def _select_recommended_model(
    info: object,
    ranked: list[tuple[dict, float | None]],
    refs: list[str],
    installations: list[recommend_status.InstallationStatus],
) -> str | None:
    import questionary

    recommend_ui.set_no_color(_global_opts().no_color)
    rows = recommend_ui.build_rows(ranked, refs, installations)
    recommend_ui.print_screen(
        console,
        info,
        len(rows),
        show_caution=any(row.warning for row in rows),
    )
    choices = [
        questionary.Choice(
            title=recommend_ui.choice_title(row, console.size.width),
            value=row.value,
        )
        for row in rows
    ]
    selected = _ask_select(
        questionary.select(
            "Choose a model",
            choices=choices,
            qmark="◆",
            pointer="❯",
            instruction="(↑↓ move · Enter select · Esc cancel)",
            style=recommend_ui.SELECT_STYLE,
        )
    )
    if selected is not None:
        selected_row = next(row for row in rows if row.value == selected)
        recommend_ui.print_detail(console, info, selected_row)
    return selected


def _print_recommend_json(
    ranked: list[tuple[dict, float | None]],
    refs: list[str],
    installations: list[recommend_status.InstallationStatus],
    profile: str,
) -> None:
    rows = recommend_ui.build_rows(ranked, refs, installations)
    console.print_json(
        data=[
            {
                "rank": index + 1,
                "ref": row.value,
                "name": row.display_name,
                "predicted_tokens_per_second": row.speed,
                "memory_required_gb": row.memory_gb,
                "use_case": row.use_case,
                "description": row.description,
                "warning": row.warning,
                "installed": row.installation.installed,
                "managed_by_omm": row.installation.managed_by_omm,
                "installed_engines": list(row.installation.engines),
                "installation_match": row.installation.match_kind,
                "profile": profile,
            }
            for index, row in enumerate(rows)
        ]
    )


def _first_uninstalled_ref(
    refs: list[str], installations: list[recommend_status.InstallationStatus]
) -> str | None:
    return next(
        (
            ref
            for ref, installation in zip(refs, installations)
            if not installation.installed
        ),
        None,
    )


def _finish_recommendation(
    selected: str,
    ranked: list[tuple[dict, float | None]],
    refs: list[str],
    installations: list[recommend_status.InstallationStatus],
) -> None:
    index = refs.index(selected)
    installation = installations[index]
    if not installation.installed:
        install(selected)
        return

    display_name = recommend_ui.humanize_model_name(ranked[index][0])
    if installation.managed_by_omm:
        message = (
            "has the same model and parameter size as an OMM-installed model"
            if installation.match_kind == "model_identity"
            else "is already installed via OMM"
        )
        console.print(f"[success]{display_name} {message}.[/success]")
        if installation.managed_filename:
            console.print(
                f"Run it now: [accent]omm run {installation.managed_filename}[/accent]"
            )
        return

    labels = [_engine_label(engine) for engine in installation.engines]
    location = f" in {', '.join(labels)}" if labels else ""
    qualifier = (
        "the same model and parameter size as an installed model"
        if installation.match_kind == "model_identity"
        else "already installed"
    )
    console.print(f"[success]{display_name} is {qualifier}{location}.[/success]")


_PROFILE_LABELS = {
    "dedicated": "Dedicated - largest model that fits, other work will be slow",
    "balanced": "Balanced - leaves room to use the computer while chatting",
    "minimal": "Minimal footprint - prioritize multitasking over model size",
}


def _ask_recommend_profile() -> str | None:
    import questionary

    choices = [
        questionary.Choice(title=_PROFILE_LABELS[p], value=p) for p in predictor.RECOMMEND_PROFILES
    ]
    return _ask_select(
        questionary.select(
            "How should this computer be shared with the LLM?",
            choices=choices,
            qmark="◆",
            pointer="❯",
            instruction="(↑↓ move · Enter select · Esc cancel)",
        )
    )


@app.command()
@global_flags
def recommend(
    profile: str = typer.Option(
        None,
        "--profile",
        help="How much of the machine to claim: dedicated, balanced, or minimal. "
        "Prompted for interactively when omitted; defaults to balanced under --yes/--json.",
    ),
) -> None:
    """Suggest a model to install for this hardware.

    Ranked by a model trained on real install telemetry, falling back to
    the static rules when that trained model can't be fetched.

    --json prints the ranked candidates and their local installation state,
    and installs nothing. --yes skips the interactive picker and installs
    the highest-ranked candidate that is not already installed."""
    import requests

    info = scan_hardware()
    config = load_config()
    json_output = _global_opts().json
    auto_yes = _global_opts().yes

    if profile is not None:
        profile = profile.casefold()
        if profile not in predictor.RECOMMEND_PROFILES:
            err_console.print(
                f"[error]--profile must be one of: {', '.join(predictor.RECOMMEND_PROFILES)}.[/error]"
            )
            raise typer.Exit(1)
    elif not json_output and not auto_yes and _stdin_is_tty():
        profile = _ask_recommend_profile()
        if profile is None:
            err_console.print("[warning]Cancelled.[/warning]")
            raise typer.Exit(0)
    else:
        profile = predictor.DEFAULT_RECOMMEND_PROFILE

    artifact, changed = _load_recommendation_with_change_note(config)
    if changed and not _global_opts().quiet and not json_output:
        console.print("[muted]Fetched updated recommendation data from GitHub.[/muted]")
    if artifact and artifact.get("candidates"):
        ranked = predictor.rank_candidates(artifact, info)
        usable = [
            (c, speed) for c, speed in ranked if speed >= predictor.MIN_USABLE_TOKENS_PER_SECOND
        ]
        within_profile = predictor.filter_by_profile(usable, info, profile)
        if within_profile:
            within_profile.sort(
                key=lambda pair: predictor.estimate_required_memory_gb(pair[0]) or 0.0,
                reverse=True,
            )
            viable = within_profile[:10]
        elif usable:
            # Nothing in the usable set clears the profile's RAM ceiling -
            # relax the profile rather than show nothing.
            if not _global_opts().quiet and not json_output:
                console.print(
                    f"[muted]No model both meets the speed floor and fits the '{profile}' "
                    "profile - showing the best fit anyway.[/muted]"
                )
            usable.sort(
                key=lambda pair: predictor.estimate_required_memory_gb(pair[0]) or 0.0,
                reverse=True,
            )
            viable = usable[:10]
        else:
            # Nothing clears the usable-speed floor (very weak hardware) - fall
            # back to the fastest candidates available rather than show nothing.
            viable = [(c, speed) for c, speed in ranked if speed > 0][:10]
        if not viable:
            err_console.print("[error]No model is predicted to run on this hardware.[/error]")
            raise typer.Exit(1)

        refs = [search_mod.exact_install_ref(c) for c, speed in viable]
        installations = recommend_status.detect_installation_statuses(
            [candidate for candidate, _speed in viable]
        )
        session_cache.record_seen(refs)
        if json_output:
            _print_recommend_json(viable, refs, installations, profile)
            return
        if auto_yes:
            selected = _first_uninstalled_ref(refs, installations)
            if selected is None:
                console.print("[success]All recommended models are already installed.[/success]")
                return
        else:
            selected = _select_recommended_model(info, viable, refs, installations)
            if selected is None:
                err_console.print("[warning]Cancelled.[/warning]")
                raise typer.Exit(0)
        _finish_recommendation(selected, viable, refs, installations)
        return

    if not _global_opts().quiet and not json_output:
        console.print("[muted]No trained model available, falling back to static rules.[/muted]")
    rules_url = config.get("rules_url")
    if rules_url:
        try:
            _, rules_changed = rules_mod.refresh_rules_with_change_note(rules_url)
            if rules_changed and not _global_opts().quiet and not json_output:
                console.print("[muted]Fetched updated rules from GitHub.[/muted]")
        except (requests.RequestException, ValueError):
            pass

    has_gpu = info.vram_total_gb is not None
    available_gb = min(
        calculate_memory_budget(info).install_budget_gb,
        predictor.profile_memory_cap_gb(info, profile),
    )

    rule_list = rules_mod.load_rules()
    matches = rules_mod.matching_rules(rule_list, available_gb, has_gpu=has_gpu)

    if not matches:
        err_console.print("[error]No model in the current rules fits this hardware.[/error]")
        raise typer.Exit(1)

    ranked_rules = [(rule, None) for rule in matches]
    refs = [rule["name"] for rule in matches]
    installations = recommend_status.detect_installation_statuses(matches)
    session_cache.record_seen(refs)
    if json_output:
        _print_recommend_json(ranked_rules, refs, installations, profile)
        return
    if auto_yes:
        selected = _first_uninstalled_ref(refs, installations)
        if selected is None:
            console.print("[success]All recommended models are already installed.[/success]")
            return
    else:
        selected = _select_recommended_model(info, ranked_rules, refs, installations)
        if selected is None:
            err_console.print("[warning]Cancelled.[/warning]")
            raise typer.Exit(0)

    _finish_recommendation(selected, ranked_rules, refs, installations)


def _print_runtime_profile(profile: tuning.RuntimeProfile) -> None:
    table = _table(title=f"Recommended {profile.profile_name} runtime profile")
    table.add_column("Setting", style="label")
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
        "[muted]These are conservative starting values; benchmark before "
        "treating them as optimal.[/muted]"
    )


@app.command()
@global_flags
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
            resolved = _resolve_model_interactive(model_name)
        except ModelResolutionError as error:
            err_console.print(f"[error]{error}[/error]")
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


def _resolve_ref(arg: str) -> str:
    """If `arg` is a bare integer, treat it as a 1-based index into the last
    `omm search`/`omm list` results shown in this terminal. Any non-numeric
    arg passes through unchanged."""
    if not arg.isdigit():
        return arg

    results = session_cache.load_last_results()
    if not results:
        err_console.print(
            "[error]Run `omm search` or `omm list` first to install/uninstall by number.[/error]"
        )
        raise typer.Exit(1)

    idx = int(arg)
    if idx < 1 or idx > len(results):
        err_console.print(f"[error]No result #{idx} (1-{len(results)}).[/error]")
        raise typer.Exit(1)

    return results[idx - 1]


def _resolve_benchmark_tag(arg: str) -> str:
    """Like `_resolve_ref`, but a numbered ref names a filename from the last
    `omm search`/`omm list`, which `omm benchmark` needs as an Ollama tag."""
    if not arg.isdigit():
        # A registry filename (what `omm list`/`omm info` show, what users
        # paste) is not an Ollama tag: passing it through verbatim made
        # `omm benchmark <name>.gguf` ask Ollama for a model called
        # "<name>.gguf" and report model_load_failed (promo dry run,
        # 2026-08-23). Map it the same way a numbered ref is mapped; any
        # other string is still treated as a literal tag.
        reg = registry.load_registry()
        filename, entry = _lookup_entry(arg, reg)
        if entry is None:
            return arg
        if not any(
            isinstance(entry.get(field), str) and entry[field].strip()
            for field in ("ollama_runtime_name", "ollama_name")
        ):
            err_console.print(f"[error]{filename} has no Ollama tag; link it with `omm link` first.[/error]")
            raise typer.Exit(1)
        return linker.resolve_ollama_runtime_name(filename, entry)
    filename = _resolve_ref(arg)
    entry = registry.load_registry().get(filename)
    if not entry or not any(
        isinstance(entry.get(field), str) and entry[field].strip()
        for field in ("ollama_runtime_name", "ollama_name")
    ):
        err_console.print(f"[error]{filename} has no Ollama tag; link it with `omm link` first.[/error]")
        raise typer.Exit(1)
    return linker.resolve_ollama_runtime_name(filename, entry)


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


def _resolve_model_interactive(model_name: str) -> ResolvedModel:
    """`resolve_model()`, but the two "which one did you mean?" outcomes walk
    the user through a picker instead of dead-ending on an error message: a
    bare `org/repo` that exists on both HuggingFace and ModelScope asks which
    provider, and a repo holding several quants asks which file. `omm search`
    prints (and caches, for numbered refs) exactly those bare `org/repo`
    names, so every command that takes a model name has to be able to finish
    the job from one - not just `omm install`.

    Escaping a picker exits 0 with "Cancelled.". Every other
    ModelResolutionError propagates: only the caller knows what its own
    failure text and suggestions should be.
    """
    import questionary

    # Two rounds at most: picking a provider can surface a quant choice, but
    # a quant choice is fully qualified, so nothing can still be ambiguous
    # afterwards. Anything beyond that is a picker failing to converge, and
    # the final call below lets its error surface instead of looping.
    for _ in range(2):
        try:
            return resolve_model(model_name)
        except AmbiguousProviderError as e:
            choices = [
                questionary.Choice(title=provider, value=provider) for provider in e.providers
            ]
            chosen_provider = _ask_select(
                questionary.select(
                    f"'{e.repo_id}' found on multiple providers, pick one:", choices=choices
                )
            )
            if chosen_provider is None:
                err_console.print("[warning]Cancelled.[/warning]")
                raise typer.Exit(0) from e
            model_name = f"{chosen_provider}:{e.repo_id}"
        except AmbiguousModelError as e:
            chosen = _pick_quant_variant(e)
            if chosen is None:
                err_console.print("[warning]Cancelled.[/warning]")
                raise typer.Exit(0) from e
            model_name = f"{e.provider}:{e.repo_id}:{chosen}"
    return resolve_model(model_name)


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
    needs_fetch = [variant for variant in variants if variant.required_gb is None]
    sizes: dict[str, int | None] = {}
    if needs_fetch:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(needs_fetch))) as executor:
            fetched = executor.map(
                lambda v: remote_file_size(error.provider, error.repo_id, v.filename),
                needs_fetch,
            )
        sizes = {v.filename: size for v, size in zip(needs_fetch, fetched)}

    resolved_variants = []
    for variant in variants:
        if variant.required_gb is not None:
            resolved_variants.append(variant)
            continue
        size_bytes = sizes.get(variant.filename)
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


def _link_model(
    dest, repo_id: str | None, ollama_tag: str, *, only_engine: str | None = None
) -> dict[str, bool]:
    """Link a downloaded .gguf into every installed engine, printing a
    warning only when an installed engine fails to link (uninstalled
    engines are skipped silently). Shared by `install` and `update` since
    both need the exact same behavior after a fresh (or refreshed) download.

    `only_engine` restricts linking to a single engine key - `omm
    contribute` only needs the one engine it's benchmarking against this
    session, so linking into every other installed engine for every
    downloaded candidate is unnecessary churn."""
    linked = {spec.key: False for spec in linker.ENGINES}

    for spec in linker.ENGINES:
        if only_engine is not None and spec.key != only_engine:
            continue
        if not linker.is_engine_installed(spec.key):
            continue
        try:
            warning = linker.link_engine(spec.key, dest, repo_id=repo_id, ollama_tag=ollama_tag)
            linked[spec.key] = True
            if warning:
                err_console.print(f"[warning]{warning}[/warning]")
        except linker.InsufficientLinkSpaceError:
            # Roll back links created earlier in this install transaction.
            # The central GGUF is removed by _install_impl only when that
            # same transaction downloaded it.
            cleanup_entry = {"repo_id": repo_id, "ollama_name": ollama_tag}
            for previous in reversed(linker.ENGINES):
                if not linked.get(previous.key):
                    continue
                try:
                    linker.unlink_engine(previous.key, dest.name, cleanup_entry)
                except (linker.LinkError, OSError):
                    pass
            raise
        except linker.LinkError as e:
            err_console.print(f"[warning]{spec.label} link skipped: {e}[/warning]")

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
    skipped_low_disk: bool = False
    sha256: str | None = None
    failure_reason: str | None = None
    model_metadata: dict | None = None
    compatibility_engine: str | None = None
    compatibility_status: str | None = None
    runtime_load_declined: bool = False
    benchmark_engine: str | None = None


class InstallInterrupted(Exception):
    """Esc fired mid-download or mid-benchmark inside `_install_impl`,
    whether that's a single `omm install` or `omm contribute`'s loop."""

    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self.filename = filename


class _Interrupted(Exception):
    pass


_CONTRIBUTE_EVALUATION_DEADLINE_SECONDS = 10 * 60


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


def _sample_background_cpu_load() -> float | None:
    """Read, and warn about, other programs' CPU use before a benchmark starts.

    Sustained background load depresses every speed sample by about the same
    amount, so the run stays internally tight and the existing dispersion
    signals - tokens_per_sec_min/max and the v9 MAD/median ratio - report it
    as a clean measurement of a machine that is simply slower than it is.
    A reading taken here, while omm is not generating anything, is the only
    place that distinction can be drawn.

    Returns the percentage so a caller can record it alongside the
    measurement, or None when it could not be read - which callers must treat
    as unknown, never as idle. Best-effort and advisory: it never blocks the
    benchmark.
    """
    percent = sample_cpu_utilization_percent()
    if percent is None or percent < BUSY_CPU_PERCENT:
        return percent
    err_console.print(
        f"[warning]Other programs are using about {percent:.0f}% of this machine's "
        "CPU. Background load lowers decode speed without widening the spread "
        "between speed samples, so this benchmark can read low while still "
        "looking internally consistent. Close heavy programs and re-run for a "
        "number comparable to an idle machine.[/warning]"
    )
    return percent


def _cpu_load_is_high(percent: float | None) -> bool:
    """Whether a sampled reading counts as background load.

    None means the reading was unavailable. That is not evidence of load, so
    it does not suppress calibration and does not label the uploaded row -
    it simply leaves the question unanswered.
    """
    return percent is not None and percent >= BUSY_CPU_PERCENT


def _background_cpu_load_is_high() -> bool:
    """Sample and warn, reporting only whether the host was busy."""
    return _cpu_load_is_high(_sample_background_cpu_load())


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
    try:
        factor = calibration.record_calibration(
            hardware,
            measured_tokens_per_sec=tokens_per_sec,
            predicted_tokens_per_sec=predicted,
            engine="ollama",
        )
    except OSError:
        return
    console.print(
        f"[muted]Local calibration updated: correction ×{factor:.2f} "
        "(the calibration stays in ~/.omm and is never uploaded).[/muted]"
    )


def _fetch_sibling_candidates(boundary: dict) -> list[dict]:
    """Phase C helper for `omm contribute`: given the candidate dict that
    was actually benchmarked at the fit/unfit boundary, look up every
    other GGUF quantization in the same repo and hand back the unseen
    ones closest to that quant level first, so the boundary search steps
    outward one quant at a time instead of jumping to an extreme.
    Best-effort - never raises, so it can't abort the contribution loop."""
    provider = boundary.get("provider") or "huggingface"
    repo_id = boundary["repo_id"]
    tried_bits = parse_quant_bits(boundary["filename"])
    if tried_bits is None:
        return []
    try:
        filenames, _ = fetch_repo_files(provider, repo_id)
    except ModelResolutionError:
        return []

    scored = []
    for filename in filenames:
        if filename == boundary["filename"] or is_mmproj_filename(filename):
            continue
        bits = parse_quant_bits(filename)
        if bits is None:
            continue
        scored.append((abs(bits - tried_bits), filename))
    scored.sort(key=lambda item: item[0])

    from concurrent.futures import ThreadPoolExecutor

    filenames = [filename for _, filename in scored]
    if filenames:
        with ThreadPoolExecutor(max_workers=min(8, len(filenames))) as executor:
            sizes = list(
                executor.map(lambda fn: remote_file_size(provider, repo_id, fn), filenames)
            )
    else:
        sizes = []

    siblings = []
    for filename, size_bytes in zip(filenames, sizes):
        candidate = dict(boundary)
        candidate["provider"] = provider
        candidate["filename"] = filename
        candidate.pop("quant_bits", None)
        candidate["size_bytes"] = size_bytes
        siblings.append(candidate)
    return siblings


@dataclass
class _VolumeRequirement:
    path: Path
    bytes_needed: int = 0
    reasons: list[str] | None = None


def _ensure_install_disk_capacity(
    dest: Path,
    size_bytes: int,
    *,
    include_download: bool,
    only_engine: str | None,
) -> None:
    """Preflight the peak bytes omm may add on every affected volume."""
    if size_bytes <= 0:
        return

    groups: dict[tuple[str, str | int], _VolumeRequirement] = {}

    def add(path: Path, reason: str) -> None:
        key = linker.storage_volume_key(path)
        requirement = groups.setdefault(key, _VolumeRequirement(path=path, reasons=[]))
        requirement.bytes_needed += size_bytes
        assert requirement.reasons is not None
        requirement.reasons.append(reason)

    if include_download:
        add(dest.parent, "central model download")
    for risk in linker.disk_copy_risks(dest, only_engine=only_engine):
        add(risk.path, risk.reason)

    failures = []
    for requirement in groups.values():
        reserve = linker.disk_safety_reserve(requirement.bytes_needed)
        required = requirement.bytes_needed + reserve
        try:
            free = shutil.disk_usage(linker.disk_usage_path(requirement.path)).free
        except OSError as error:
            raise InsufficientDiskSpaceError(
                f"Could not verify free space on the volume containing {requirement.path}: {error}"
            ) from error
        if free < required:
            reasons = ", ".join(requirement.reasons or [])
            failures.append(
                f"{requirement.path} needs up to {required / 1024**3:.1f} GiB "
                f"({reasons}) but only {free / 1024**3:.1f} GiB is free"
            )

    if failures:
        raise InsufficientDiskSpaceError(
            "Not enough disk space: " + "; ".join(failures),
            fix="Free up disk space on that volume, or retry with `--skip-unfit` "
            "to skip models that don't fit.",
        )


def _run_memory_guard(
    runtime, *, target_key: str, matches_target, required_gb: float
) -> tuple[bool, object, bool]:
    """Check live memory before a load and release only OMM-owned models of
    `runtime`'s engine. Shared body for `_guard_ollama_load` and
    `_guard_lmstudio_load` - both engines follow the identical ask/block/
    observe policy semantics, differing only in how a resident is matched
    to the load target and how residents/unloads are performed."""
    latest_residents: tuple[memory_guard_mod.ResidentModel, ...] = ()
    target_preloaded = False

    def _plan():
        nonlocal latest_residents, target_preloaded
        latest_residents = runtime.list_residents()
        target_preloaded = any(matches_target(resident.model_id) for resident in latest_residents)
        candidates = tuple(
            resident for resident in latest_residents if not matches_target(resident.model_id)
        )
        return memory_guard_mod.plan_memory_guard(
            required_gb,
            scan_hardware(),
            candidates,
        )

    plan = _plan()
    # A resident target will not allocate a second model-sized footprint, and
    # must never be selected as reclamation collateral under an alias.
    if target_preloaded:
        return True, runtime, True

    policy = memory_guard_mod.normalize_policy(load_config().get("memory_guard_policy", "ask"))
    if plan.decision is memory_guard_mod.GuardDecision.SAFE:
        return True, runtime, False
    if policy is memory_guard_mod.GuardPolicy.OBSERVE:
        err_console.print(
            f"[warning]Memory Guard warning: {required_gb:.1f} GB requested, "
            f"{plan.available_gb:.1f} GB safely available. Observe mode will not unload anything.[/warning]"
        )
        return True, runtime, False

    def _consent(candidate_plan) -> bool:
        if not _stdin_is_tty():
            return False
        names = ", ".join(resident.model_id for resident in candidate_plan.managed_residents)
        return _ask_confirm(
            f"Memory Guard can free OMM-managed model(s) ({names}) before loading {target_key}. Continue?"
        )

    execution = memory_guard_mod.execute_guard(
        plan,
        policy,
        runtime,
        consent=_consent,
        recalculate=_plan,
    )
    if not execution.allowed:
        if execution.unloaded:
            err_console.print(
                "[warning]Memory Guard already released OMM-managed model(s): "
                + ", ".join(resident.model_id for resident in execution.unloaded)
                + ".[/warning]"
            )
        err_console.print(
            f"[error]Memory Guard blocked the load: {required_gb:.1f} GB requested, "
            f"{plan.available_gb:.1f} GB safely available ({', '.join(execution.reasons)}).[/error]"
        )
        return False, runtime, False
    if execution.unloaded:
        console.print(
            "[success]Memory Guard released and verified OMM-managed model(s): "
            + ", ".join(resident.model_id for resident in execution.unloaded)
            + ".[/success]"
        )
    return True, runtime, False


def _guard_ollama_load(
    tag: str, required_gb: float
) -> tuple[bool, memory_guard_mod.OllamaManagedRuntime, bool]:
    """Check live memory before an Ollama load and release only OMM-owned models."""
    runtime = memory_guard_mod.OllamaManagedRuntime(registry.load_registry())
    return _run_memory_guard(
        runtime,
        target_key=tag,
        matches_target=lambda resident_id: memory_guard_mod._same_ollama_id(resident_id, tag),
        required_gb=required_gb,
    )


def _guard_contribution_load(
    tag: str,
    dest: Path,
    runtime_hw: HardwareInfo,
    runtime_profile: tuning.RuntimeProfile,
    runtime_options: dict,
    prior_estimate: contribute_memory.ContributionMemoryEstimate | None,
) -> tuple[
    bool,
    bool,
    contribute_memory.ContributionMemoryEstimate | None,
    float,
    str | None,
]:
    """Revalidate a contribution load using the downloaded GGUF header.

    Only other OMM-owned residents are released. A user-preloaded target is
    never double-counted or unloaded.
    """
    from omm.gguf import read_gguf_metadata

    runtime = memory_guard_mod.OllamaManagedRuntime(registry.load_registry())
    residents = runtime.list_residents()
    target_preloaded = any(
        memory_guard_mod._same_ollama_id(resident.model_id, tag)
        for resident in residents
    )
    metadata: dict[str, object] = {}
    try:
        metadata.update(read_gguf_metadata(dest, {"general.architecture"}))
        architecture = metadata.get("general.architecture")
        if isinstance(architecture, str) and architecture:
            metadata.update(
                read_gguf_metadata(
                    dest,
                    contribute_memory.metadata_keys_for_architecture(architecture),
                )
            )
    except (OSError, ValueError, KeyError, struct.error):
        metadata = {}
    if runtime_options.get("num_gpu") != 0:
        resolved_options, actual_offload = tuning.contribute_ollama_options(
            runtime_profile, metadata
        )
        runtime_options.clear()
        runtime_options.update(resolved_options)
    else:
        actual_offload = 0
    estimate = contribute_memory.estimate_candidate_memory(
        {"size_bytes": dest.stat().st_size},
        runtime_hw,
        context_length=runtime_profile.context_length,
        num_batch=runtime_profile.num_batch,
        gpu_offload_percent=actual_offload,
        metadata=metadata,
        mmap_weights=contribute_memory.weights_mmap_expected(runtime_hw),
    ) or prior_estimate

    for resident in residents:
        if (
            resident.owned_by_omm
            and not memory_guard_mod._same_ollama_id(resident.model_id, tag)
        ):
            runtime.unload(resident)

    fresh_hw = scan_hardware()
    sample = contribute_memory.sample_available_memory(
        available_ram_gb,
        total_ram_gb=fresh_hw.ram_total_gb,
        sample_count=3,
        interval_seconds=0.1,
    )
    if target_preloaded or estimate is None:
        return True, target_preloaded, estimate, sample.reserve_gb, None
    plan = contribute_memory.plan_candidate_memory(
        estimate,
        fresh_hw,
        sample,
        commit=windows_commit_info(),
    )
    if plan.decision is contribute_memory.ContributionMemoryDecision.SAFE:
        return True, False, estimate, sample.reserve_gb, None
    reason = (
        "memory_allocation_blocked"
        if plan.decision is contribute_memory.ContributionMemoryDecision.BLOCK
        else "memory_allocation_deferred"
    )
    return False, False, estimate, sample.reserve_gb, reason


def _guard_lmstudio_load(
    model_key: str, required_gb: float
) -> tuple[bool, memory_guard_mod.LMStudioManagedRuntime, bool]:
    """Check live memory before an LM Studio load and release only OMM-owned models."""
    runtime = memory_guard_mod.LMStudioManagedRuntime(registry.load_registry())
    return _run_memory_guard(
        runtime,
        target_key=model_key,
        matches_target=lambda resident_id: resident_id.casefold() == model_key.casefold(),
        required_gb=required_gb,
    )


def _guard_engine_load(engine: str, target_key: str, required_gb: float) -> tuple[bool, object, bool]:
    """Dispatch to the engine-specific memory guard."""
    if engine == "ollama":
        return _guard_ollama_load(target_key, required_gb)
    if engine == "lmstudio":
        return _guard_lmstudio_load(target_key, required_gb)
    raise ValueError(f"unsupported guard engine: {engine}")


def _post_install_runtime(linked: dict[str, bool], preferred: str | None) -> str | None:
    """Select one linked local runtime without starting any application."""
    available = [engine for engine in ("ollama", "lmstudio") if linked.get(engine)]
    if preferred in available:
        return preferred
    reachable = []
    for engine in available:
        try:
            if _compatibility_adapter(engine).health().reachable:
                reachable.append(engine)
        except (RuntimeAdapterError, ValueError):
            continue
    if reachable:
        return reachable[0]
    return available[0] if available else None


def _engine_daemon_reachable(engine: str) -> bool:
    """Dispatch to the engine-specific "is the daemon already up" check."""
    if engine == "lmstudio":
        return linker.lmstudio_daemon_reachable()
    return benchmark.ollama_daemon_reachable()


def _start_engine_daemon(engine: str):
    """Best-effort daemon start. Returns an opaque handle for
    `_stop_engine_daemon` - an Ollama Popen for "ollama", or a bool
    sentinel for "lmstudio" (LM Studio's lifecycle is a named background
    service with no process handle to hand back, per
    linker.start_lmstudio_daemon's contract). None means the daemon could
    not be confirmed running, matching benchmark.start_ollama_daemon's
    existing "None means failed" contract."""
    if engine == "lmstudio":
        return True if linker.start_lmstudio_daemon() else None
    return benchmark.start_ollama_daemon()


def _stop_engine_daemon(engine: str, handle) -> None:
    """Counterpart to `_start_engine_daemon`; only ever called with a
    non-None handle, mirroring the existing single-engine call sites."""
    if engine == "lmstudio":
        linker.stop_lmstudio_daemon()
    else:
        benchmark.stop_ollama_daemon(handle)


def _engine_version(engine: str) -> str | None:
    """Live daemon version string for telemetry's `engine_version` field.
    Mirrors quality.collect_evidence's inline LM Studio version lookup
    (Task 2) - no shared helper exists in quality.py for this, so the same
    small pattern is intentionally repeated here rather than adding one
    there."""
    if engine == "lmstudio":
        port = linker.lmstudio_server_port()
        if port is None:
            return None
        return LMStudioAdapter(base_url=f"http://127.0.0.1:{port}").health().version
    return quality_mod.ollama_version()


def _engine_label(engine: str) -> str:
    """Product name for an engine key, for use in messages the user reads:
    "lmstudio" is a registry key, "LM Studio" is what the program is called.

    Reads linker.ENGINES on every call rather than caching a dict at import
    time, so a test monkeypatching the engine list still takes effect (same
    reason linker.is_engine_installed avoids a module-level lookup table).
    Unknown keys fall back to Ollama, matching the engine keys' own default
    across the CLI."""
    for spec in linker.ENGINES:
        if spec.key == engine:
            return spec.label
    return "Ollama"


def _print_engine_selection_notice(engine: str) -> None:
    """`_select_benchmark_engine` picks LM Studio when Ollama is unavailable,
    and nothing downstream ever says so - prompts, progress, and results read
    identically either way, so a user with both engines installed cannot tell
    which one produced their numbers. Announces the fallback only: Ollama is
    the documented default, so naming it on every run would be noise rather
    than information."""
    if engine != "lmstudio" or _global_opts().quiet:
        return
    console.print(
        f"[muted]Ollama isn't installed or running - using {_engine_label(engine)} "
        "instead.[/muted]"
    )


def _memory_pressure_report_lines(engine: str, cancelled: bool) -> tuple[str, str]:
    """Verdict plus follow-up for a run Memory Guard stopped under sustained
    low memory. The verdict on its own leaves the user with nothing to do
    about it, and the two branches need different advice: a confirmed
    cancellation means the weights were released and only the machine's
    other memory pressure is left to fix, while an unconfirmed one means the
    engine may still be holding them - which no amount of closing other apps
    will release."""
    label = _engine_label(engine)
    if cancelled:
        return (
            "[error]Memory Guard detected sustained low memory and cancelled "
            "OMM's model operation.[/error]",
            "[muted]Close memory-heavy apps to free RAM, then try again.[/muted]",
        )
    return (
        "[error]Memory Guard detected sustained low memory and could not confirm "
        "cancellation of OMM's model operation.[/error]",
        f"[muted]{label} may still be holding the model in memory. Restart "
        f"{label}, close memory-heavy apps, then try again.[/muted]",
    )


def _select_benchmark_engine() -> str | None:
    """"ollama" if Ollama's daemon can be reached or started, else
    "lmstudio" if LM Studio's can, else None (caller must error out with an
    actionable message - neither engine is usable).

    Only checks availability/reachability; never starts a daemon itself -
    the caller starts (and later stops) whichever engine wins, at the same
    points the existing single-engine flow already did."""
    if benchmark.find_ollama_executable() is not None or benchmark.ollama_daemon_reachable():
        return "ollama"
    if linker._lms_cli_path() is not None or linker.lmstudio_daemon_reachable():
        return "lmstudio"
    return None


def _ensure_engine_running(
    engine: str, action: str, *, assume_yes: bool = False
) -> tuple[str, object]:
    """Preflight the selected engine's daemon, prompting to start it if
    installed-but-stopped. `engine` should already come from
    `_select_benchmark_engine()`, so "is this engine usable at all" is not
    re-checked here - only "is it already running, and if not, do we have
    permission to start it."

    Returns (actual_engine, handle): `handle` is an opaque handle for
    `_stop_engine_daemon`, or None if nothing needed starting. `actual_engine`
    differs from the requested `engine` only when Ollama was picked but its
    daemon could not be started (or the user declined to start it) and LM
    Studio is available as a fallback - `_select_benchmark_engine` only
    checks that Ollama's executable exists or its daemon is already
    reachable, not that a stopped daemon can actually come up, so this is the
    first point that can discover it can't."""
    if engine == "ollama":
        try:
            return "ollama", _ensure_ollama_running(action, assume_yes=assume_yes)
        except typer.Exit:
            if linker._lms_cli_path() is None and not linker.lmstudio_daemon_reachable():
                raise
            console.print(
                f"[muted]Ollama isn't available for `omm {action}` - falling back to "
                "LM Studio instead.[/muted]"
            )
    return "lmstudio", _ensure_lmstudio_running(action, assume_yes=assume_yes)


def _ensure_lmstudio_running(action: str, *, assume_yes: bool = False):
    """Preflight LM Studio's local server, prompting to start it if
    installed-but-stopped. Same contract as `_ensure_ollama_running`: None
    means it was already reachable, a truthy handle means this call started
    it (pass to `_stop_engine_daemon("lmstudio", ...)` afterward), and a
    typer.Exit(1) means it isn't available or the user declined."""
    if linker.lmstudio_daemon_reachable():
        return None
    prompt = f"LM Studio is installed but its server isn't running. Start it now for `omm {action}`?"
    if not assume_yes and (not _stdin_is_tty() or not _ask_confirm(prompt)):
        err_console.print(
            f"[error]omm {action} requires LM Studio's local server to be running.[/error]"
        )
        raise typer.Exit(1)
    started = linker.start_lmstudio_daemon()
    if not started:
        err_console.print("[error]Could not start LM Studio's local server.[/error]")
        raise typer.Exit(1)
    return True


def _record_install_compatibility(
    filename: str,
    engine: str,
    *,
    status: str,
    runtime_version: str | None,
    failure_reason: str | None,
) -> None:
    result = CompatibilityResult(
        engine=engine,
        status=status,
        checked_at=datetime.now(timezone.utc).isoformat(),
        probe_version=PROBE_VERSION,
        runtime_version=runtime_version,
        failure_reason=failure_reason,
    )
    registry.record_compatibility(filename, engine, result.registry_payload())


def _verify_lmstudio_after_install(
    filename: str,
    entry: dict,
    *,
    allow_load: bool | None,
    enforce_memory_guard: bool = False,
) -> tuple[str | None, bool, bool]:
    """Run the bounded LM Studio probe; return (status, consent_declined, guard_blocked)."""
    adapter = _compatibility_adapter("lmstudio")
    model_ref = _compatibility_model_ref(filename, entry, "lmstudio")
    health = adapter.health()
    if not health.reachable:
        result = verify_and_record(filename, adapter, model_ref)
        reason = result.failure_reason or "server_unavailable"
        err_console.print(
            "[warning]LM Studio compatibility could not be verified: "
            f"{_COMPATIBILITY_FAILURE_MESSAGES.get(reason, reason)}. "
            "The downloaded model was kept.[/warning]"
        )
        return result.status, False, False
    model_loaded = False
    try:
        visible = find_runtime_model(adapter.list_models(), model_ref)
        model_loaded = bool(visible and visible.loaded)
    except RuntimeAdapterError:
        pass
    if not model_loaded:
        consent = allow_load
        if consent is None:
            consent = _ask_confirm(
                f"Load {filename} into LM Studio memory for a short local test?"
            )
        if not consent:
            err_console.print(
                "[warning]Runtime verification skipped; the model was not loaded.[/warning]"
            )
            return None, True, False
        if enforce_memory_guard:
            size_bytes = entry.get("size_bytes")
            if not _positive_finite_number(size_bytes):
                try:
                    size_bytes = _managed_model_path(filename).stat().st_size
                except (ModelResolutionError, OSError):
                    size_bytes = None
            if not _positive_finite_number(size_bytes):
                err_console.print(
                    "[error]Memory Guard could not determine the model size; "
                    "the LM Studio load was blocked.[/error]"
                )
                _record_install_compatibility(
                    filename,
                    "lmstudio",
                    status="failed",
                    runtime_version=health.version,
                    failure_reason="memory_guard_blocked",
                )
                return "failed", False, True
            guard_allowed, _guard_runtime, _preloaded = _guard_lmstudio_load(
                model_ref.key,
                float(size_bytes) / (1024**3) * 1.2,
            )
            if not guard_allowed:
                _record_install_compatibility(
                    filename,
                    "lmstudio",
                    status="failed",
                    runtime_version=health.version,
                    failure_reason="memory_guard_blocked",
                )
                return "failed", False, True

    console.print(f"Verifying {filename} with {_engine_label('lmstudio')}...")
    result = verify_and_record(filename, adapter, model_ref)
    if result.status == "passed":
        console.print(
            "[success]LM Studio compatibility verified; the test load was released.[/success]"
        )
    else:
        reason = result.failure_reason or "unknown"
        err_console.print(
            "[warning]LM Studio compatibility could not be verified: "
            f"{_COMPATIBILITY_FAILURE_MESSAGES.get(reason, reason)}. "
            "The downloaded model was kept.[/warning]"
        )
    return result.status, False, False


def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    no_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
    use_quality_eval: bool = False,
    quality_pack: dict | None = None,
    link_only_engine: str | None = None,
    assume_yes: bool = False,
    force: bool = False,
    verify_runtime_after_install: bool = False,
    runtime_load_consent: bool | None = None,
    preferred_runtime: str | None = None,
    enforce_memory_guard: bool = False,
    gpu_state: dict | None = None,
    benchmark_engine: str = "ollama",
    contribute_mode: bool = False,
    contribution_memory_estimate: contribute_memory.ContributionMemoryEstimate | None = None,
) -> InstallOutcome:
    """Core of `omm install`: download, link, register, benchmark+calibrate
    automatically, optionally report telemetry. Shared by the plain
    `install` command and `omm contribute`'s unattended loop via the
    kwargs above.

    `benchmark_engine` only matters when `use_quality_eval=True` (the only
    caller that sets it to anything but the default is `omm contribute`'s
    unattended loop) - it selects which engine's daemon/eval/telemetry path
    runs. Plain `omm install` never passes it, so it always stays "ollama"
    there and every code path below behaves exactly as before this engine
    parameter existed."""
    opts = _global_opts()
    url, filename, repo_id = resolved.url, resolved.filename, resolved.repo_id
    try:
        filename = validate_model_filename(filename)
        provider = validate_provider(resolved.provider or "huggingface")
        if repo_id is not None:
            repo_id = validate_repo_id(repo_id)
    except ModelResolutionError as error:
        raise DownloadError(str(error)) from error
    try:
        dest = _managed_model_path(filename)
    except ModelResolutionError as error:
        raise DownloadError(str(error)) from error

    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    if trees is not None:
        hw = scan_hardware()
        candidate = {"repo_id": repo_id, "filename": filename}
        speed = predictor.predict_speed(trees, hw, candidate)
        if speed <= 0:
            err_console.print(
                f"[error]Warning: this hardware is predicted not to run {filename}.[/error]"
            )
            if skip_unfit:
                return InstallOutcome(filename, repo_id, linked={}, skipped_unfit=True)
            if not assume_yes and not _ask_confirm("Install anyway?"):
                err_console.print("[warning]Cancelled.[/warning]")
                raise typer.Exit(0)
        else:
            try:
                _, speed_low, speed_high = predictor.predict_speed_interval(trees, hw, candidate)
            except (ValueError, KeyError, TypeError, IndexError):
                speed_low = speed_high = speed
            if not opts.quiet:
                console.print(
                    f"[muted]Predicted speed: {speed:.1f} tok/s "
                    f"(range {speed_low:.1f}–{speed_high:.1f}).[/muted]"
                )

    downloaded_now = False
    expected_sha256 = resolved.expected_sha256 or (
        remote_file_sha256(provider, repo_id, filename)
        if resolved.provider and repo_id
        else None
    )
    if resolved.provider and repo_id and expected_sha256 is None:
        raise DownloadError(
            f"{provider} did not provide a SHA-256 digest for {filename}; "
            "refusing an unverifiable download."
        )
    if dest.exists() and not force:
        existing_sha256 = sha256_file(dest)
        if expected_sha256 is not None and existing_sha256 != expected_sha256:
            raise DownloadError(
                f"{filename} already exists but does not match the provider SHA-256; "
                "refusing to reuse or overwrite it."
            )
        if expected_sha256 is None:
            existing_entry = registry.load_registry().get(filename)
            if not (
                isinstance(existing_entry, dict)
                and existing_entry.get("source") == url
                and existing_entry.get("sha256") == existing_sha256
            ):
                raise DownloadError(
                    f"{filename} already exists but its source and digest cannot "
                    "be verified; refusing to adopt or overwrite it."
                )
        err_console.print(f"[warning]{filename} already downloaded, skipping fetch.[/warning]")
    else:
        if force:
            # A forced install must never reuse completed or partial bytes.
            _cleanup_incomplete_install(filename)
        size_bytes = remote_file_size(provider, repo_id, filename) if repo_id else None
        if size_bytes:
            try:
                _ensure_install_disk_capacity(
                    dest,
                    size_bytes,
                    include_download=True,
                    only_engine=link_only_engine,
                )
            except InsufficientDiskSpaceError as error:
                if skip_unfit:
                    err_console.print(f"[warning]Skipping {error}.[/warning]")
                    return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
                errors.print_cli_error(err_console, f"{error}.", fix=error.fix)
                raise typer.Exit(1) from error
        try:
            if stop_event is not None:
                download_file(
                    url, dest, stop_check=stop_event.is_set,
                    quiet=opts.quiet, no_color=opts.no_color,
                )
            else:
                download_file(url, dest, quiet=opts.quiet, no_color=opts.no_color)
            downloaded_now = True
        except DownloadCancelled as e:
            raise InstallInterrupted(filename) from e
        except InsufficientDiskSpaceError as e:
            _cleanup_incomplete_install(filename)
            errors.print_cli_error(err_console, str(e), fix=e.fix)
            if skip_unfit:
                return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
            raise typer.Exit(1) from e
        except DownloadError:
            raise

    try:
        _ensure_install_disk_capacity(
            dest,
            dest.stat().st_size,
            include_download=False,
            only_engine=link_only_engine,
        )
    except InsufficientDiskSpaceError as error:
        if downloaded_now:
            _cleanup_incomplete_install(filename)
        if skip_unfit:
            err_console.print(f"[warning]Skipping {error}.[/warning]")
            return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
        errors.print_cli_error(err_console, f"{error}.", fix=error.fix)
        raise typer.Exit(1) from error

    if not opts.quiet:
        console.print("Verifying checksum..." if expected_sha256 else "Computing checksum...")
    sha256 = sha256_file(dest)
    if expected_sha256 is not None and sha256 != expected_sha256:
        dest.unlink(missing_ok=True)
        raise DownloadError(
            f"Downloaded SHA-256 for {filename} does not match the provider metadata."
        )

    ollama_tag = linker.sanitize_ollama_tag(filename)
    try:
        linked = _link_model(dest, repo_id, ollama_tag, only_engine=link_only_engine)
    except linker.InsufficientLinkSpaceError as error:
        if downloaded_now:
            _cleanup_incomplete_install(filename)
        if skip_unfit:
            err_console.print(f"[warning]Skipping {error}[/warning]")
            return InstallOutcome(filename, repo_id, linked={}, skipped_low_disk=True)
        errors.print_cli_error(err_console, str(error), fix=error.fix)
        raise typer.Exit(1) from error

    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        source=url,
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        repo_id=repo_id,
        provider=provider,
        linked=linked,
    )

    selected_runtime = (
        _post_install_runtime(linked, preferred_runtime)
        if verify_runtime_after_install
        else None
    )
    compatibility_status = None
    runtime_load_declined = False
    lmstudio_guard_blocked = False
    if selected_runtime == "lmstudio":
        entry = registry.load_registry().get(filename, {})
        compatibility_status, runtime_load_declined, lmstudio_guard_blocked = (
            _verify_lmstudio_after_install(
                filename,
                entry,
                allow_load=runtime_load_consent,
                enforce_memory_guard=enforce_memory_guard,
            )
        )

    tokens_per_sec = None
    telemetry_sent = False
    sample_count = 1
    speed_min = speed_max = None
    speed_samples = None
    quality_summary = None
    runtime = None
    model_metadata = None
    engine_version = None
    runtime_options = None
    memory_measurement = None
    guard_failure_reason = "memory_guard_blocked" if lmstudio_guard_blocked else None
    eval_error: quality_mod.QualityEvaluationError | None = None
    run_ollama_benchmark = (
        linked["ollama"] and selected_runtime != "lmstudio" and benchmark_engine == "ollama"
    )
    # verify_runtime_after_install (plain `omm install`'s compat check) never
    # combines with benchmark_engine != "ollama" - only `omm contribute`
    # passes a non-default benchmark_engine, and it never sets
    # verify_runtime_after_install. selected_runtime stays None there, so
    # this condition reduces to "the contribute loop picked LM Studio and
    # linked it."
    run_lmstudio_benchmark = (
        linked["lmstudio"] and selected_runtime != "ollama" and benchmark_engine == "lmstudio"
    )
    model_was_preloaded = False
    ollama_runtime_version = None
    ollama_daemon_handle = None
    if run_ollama_benchmark and verify_runtime_after_install:
        adapter = _compatibility_adapter("ollama")
        health = adapter.health()
        ollama_runtime_version = health.version
        if not health.reachable and benchmark.ollama_install_state() == "stopped":
            # Installed but not running - offer to start it, same
            # start/ask/stop-when-done pattern as `_ensure_engine_running`
            # uses for contribute/benchmark, so a plain install can still
            # get a real measurement instead of only the ML prediction.
            try:
                ollama_daemon_handle = _ensure_ollama_running("install", assume_yes=assume_yes)
            except typer.Exit:
                ollama_daemon_handle = None
            if ollama_daemon_handle is not None:
                health = adapter.health()
                ollama_runtime_version = health.version
        if not health.reachable:
            _record_install_compatibility(
                filename,
                "ollama",
                status="failed",
                runtime_version=health.version,
                failure_reason=health.failure_reason or "server_unavailable",
            )
            compatibility_status = "failed"
            reason = health.failure_reason or "server_unavailable"
            err_console.print(
                "[warning]Ollama compatibility could not be verified: "
                f"{_COMPATIBILITY_FAILURE_MESSAGES.get(reason, reason)}. "
                "The downloaded model was kept.[/warning]"
            )
            run_ollama_benchmark = False
        else:
            model_ref = _compatibility_model_ref(
                filename, registry.load_registry().get(filename, {}), "ollama"
            )
            try:
                visible = find_runtime_model(adapter.list_models(), model_ref)
            except RuntimeAdapterError:
                visible = None
            model_was_preloaded = bool(visible and visible.loaded)
            if not model_was_preloaded:
                consent = runtime_load_consent
                if consent is None:
                    consent = _ask_confirm(
                        f"Load {filename} into Ollama memory for a local benchmark "
                        "and compatibility test?"
                    )
                if not consent:
                    err_console.print(
                        "[warning]Runtime verification skipped; the model was not loaded.[/warning]"
                    )
                    runtime_load_declined = True
                    run_ollama_benchmark = False
                    if ollama_daemon_handle is not None:
                        _stop_engine_daemon("ollama", ollama_daemon_handle)
                        ollama_daemon_handle = None

    if run_ollama_benchmark or run_lmstudio_benchmark:
        if run_lmstudio_benchmark and not use_quality_eval:
            # No current caller combines benchmark_engine="lmstudio" with
            # use_quality_eval=False - only `omm contribute`'s loop ever
            # sets a non-default benchmark_engine, and it always sets
            # use_quality_eval=True too. Fail loudly instead of silently
            # falling into the Ollama-only sample path below against the
            # wrong engine if that invariant is ever violated.
            raise NotImplementedError(
                "LM Studio benchmarking via _install_impl requires use_quality_eval=True"
            )
        if not opts.quiet:
            console.print("Benchmarking...")
        # Sampled here, before the daemon is started and the model is loaded,
        # so the reading describes other programs rather than omm's own work.
        host_cpu_load_percent = _sample_background_cpu_load()
        host_cpu_busy = _cpu_load_is_high(host_cpu_load_percent)
        started_daemon = ollama_daemon_handle
        pressure_watcher = None
        if (
            not verify_runtime_after_install
            and not _engine_daemon_reachable(benchmark_engine)
        ):
            started_daemon = _start_engine_daemon(benchmark_engine)
        try:
            runtime_hw = scan_hardware()
            runtime_candidate = {
                "filename": filename, "repo_id": repo_id, "size_bytes": dest.stat().st_size,
            }
            lmstudio_model = None
            lmstudio_port = None
            if benchmark_engine == "lmstudio":
                lmstudio_port = linker.lmstudio_server_port()
                lmstudio_model = linker.resolve_lmstudio_model(repo_id, filename)
                # None here means the just-linked model couldn't be
                # resolved back to an LM Studio modelKey - metadata lookup
                # and the eval call below both fail soft on this via
                # QualityEvaluationError(FAILURE_REASON_MODEL_LOAD_FAILED),
                # same shape as Ollama's own "not installed" case.
                benchmark_tag = lmstudio_model.get("model_key") if lmstudio_model else None
            else:
                benchmark_tag = ollama_tag
            try:
                if benchmark_engine == "lmstudio":
                    model_metadata = quality_mod._model_metadata(
                        benchmark_tag or filename, engine="lmstudio", lmstudio_model=lmstudio_model
                    )
                else:
                    model_metadata = quality_mod._model_metadata(ollama_tag)
                runtime_candidate.update(model_metadata)
            except quality_mod.QualityEvaluationError:
                model_metadata = None
            runtime_profile = (
                tuning.recommend_contribute_settings(runtime_hw, runtime_candidate)
                if contribute_mode
                else tuning.recommend_runtime_settings(runtime_hw, runtime_candidate)
            )
            runtime_options = runtime_profile.ollama_options
            if gpu_state is not None and gpu_state.get("force_cpu"):
                # A previous candidate this session already crashed the GPU
                # backend on this hardware (see the gpu_crash retry below) -
                # skip straight to CPU instead of re-triggering the same
                # crash on every remaining candidate. (LM Studio ignores
                # this option entirely - see quality._evaluate_tag_once's
                # docstring - so setting it is harmless there too.)
                runtime_options = dict(runtime_options)
                runtime_options["num_gpu"] = 0
            if enforce_memory_guard and not (benchmark_engine == "lmstudio" and benchmark_tag is None):
                # The precise GGUF-based estimator (_guard_contribution_load)
                # only knows how to inspect an Ollama tag; LM Studio
                # contribute sessions fall back to the coarser engine-generic
                # guard, same as a plain (non-contribute) install of either
                # engine.
                if contribute_mode and benchmark_engine == "ollama":
                    (
                        guard_allowed,
                        target_was_preloaded,
                        contribution_memory_estimate,
                        pressure_threshold_gb,
                        contribution_guard_reason,
                    ) = _guard_contribution_load(
                        ollama_tag,
                        dest,
                        runtime_hw,
                        runtime_profile,
                        runtime_options,
                        contribution_memory_estimate,
                    )
                else:
                    required_gb = runtime_profile.required_memory_gb or (
                        dest.stat().st_size / (1024**3) * 1.2
                    )
                    guard_allowed, _guard_runtime, target_was_preloaded = _guard_engine_load(
                        benchmark_engine, benchmark_tag, required_gb
                    )
                    pressure_threshold_gb = calculate_memory_budget(
                        scan_hardware()
                    ).ram_safety_reserve_gb
                    contribution_guard_reason = None
                model_was_preloaded = model_was_preloaded or target_was_preloaded
                if not guard_allowed:
                    guard_failure_reason = contribution_guard_reason or "memory_guard_blocked"
                    return InstallOutcome(
                        filename,
                        repo_id,
                        linked,
                        ollama_tag,
                        None,
                        False,
                        sha256=sha256,
                        failure_reason=guard_failure_reason,
                        model_metadata=model_metadata,
                        compatibility_engine=selected_runtime,
                        compatibility_status=compatibility_status,
                        runtime_load_declined=runtime_load_declined,
                        benchmark_engine=benchmark_engine,
                    )
                guard_config = load_config()
                pressure_watcher = memory_guard_mod.RuntimePressureWatcher(
                    memory_guard_mod.SustainedPressureMonitor(
                        pressure_threshold_gb,
                        low_memory_seconds=float(
                            guard_config.get("memory_guard_low_memory_seconds", 3.0)
                        ),
                    ),
                    sample_available_gb=available_ram_gb,
                    operation_owned_by_omm=not target_was_preloaded,
                    cancel_owned_operation=lambda: quality_mod.unload_model(
                        benchmark_tag, engine=benchmark_engine
                    ),
                    poll_seconds=float(guard_config.get("memory_guard_poll_seconds", 1.0)),
                )
                pressure_watcher.__enter__()
            if use_quality_eval:
                try:
                    if benchmark_engine == "lmstudio" and benchmark_tag is None:
                        raise quality_mod.QualityEvaluationError(
                            f"LM Studio model for '{filename}' could not be resolved to "
                            "an installed model",
                            failure_reason=quality_mod.FAILURE_REASON_MODEL_LOAD_FAILED,
                        )
                    gpu_crash_retries_left = 1
                    while True:
                        def _evaluate_with_runtime():
                            try:
                                # Ollama keeps the exact call shape it always
                                # had (no engine kwargs) so any pre-existing
                                # monkeypatch of evaluate_model that accepts
                                # runtime_options but not the newer engine
                                # kwargs still matches on the first try,
                                # instead of falling through to the
                                # compatibility branch below and silently
                                # losing runtime_options.
                                if benchmark_engine == "ollama":
                                    return quality_mod.evaluate_model(
                                        benchmark_tag, quality_pack, speed_runs=3,
                                        runtime_options=runtime_options,
                                    )
                                return quality_mod.evaluate_model(
                                    benchmark_tag, quality_pack, speed_runs=3,
                                    runtime_options=runtime_options,
                                    engine=benchmark_engine, lmstudio_port=lmstudio_port,
                                    lmstudio_model=lmstudio_model,
                                )
                            except TypeError:  # compatibility with older integrations
                                return quality_mod.evaluate_model(benchmark_tag, quality_pack, speed_runs=3)

                        try:
                            if (
                                stop_event is not None
                                and quality_mod.evaluate_model is quality_mod._DEFAULT_EVALUATE_MODEL
                            ):
                                def report_progress(elapsed: float, deadline: float) -> None:
                                    if opts.quiet:
                                        return
                                    console.print(
                                        f"[muted]Still benchmarking {filename}: {int(elapsed)}s elapsed "
                                        f"(automatic cutoff at {int(deadline)}s).[/muted]"
                                    )

                                result = quality_mod.evaluate_model_isolated(
                                    benchmark_tag,
                                    quality_pack,
                                    speed_runs=3,
                                    runtime_options=runtime_options,
                                    model_metadata=model_metadata,
                                    timeout_seconds=_CONTRIBUTE_EVALUATION_DEADLINE_SECONDS,
                                    stop_check=stop_event.is_set,
                                    progress_callback=report_progress,
                                    engine=benchmark_engine,
                                    lmstudio_port=lmstudio_port,
                                    lmstudio_model=lmstudio_model,
                                )
                            else:
                                result = _run_interruptible(_evaluate_with_runtime, stop_event)
                            break
                        except quality_mod.QualityEvaluationError as error:
                            # A crashed GPU backend (stale/mismatched CUDA or
                            # ROCm driver) is not a per-model incompatibility -
                            # a CPU-only retry of this same candidate has a
                            # real chance of succeeding. A plain
                            # unsupported_runtime (e.g. a model capability
                            # Ollama rejects) does not set gpu_crash and falls
                            # through to the normal failure path below.
                            # Ollama-only: LM Studio ignores num_gpu entirely
                            # (see quality._evaluate_tag_once's docstring), so
                            # a "retry on CPU only" here would rerun with
                            # identical behavior and just waste the one
                            # retry - a LM Studio gpu_crash falls straight
                            # through to the normal failure path below
                            # instead.
                            if (
                                benchmark_engine == "ollama"
                                and error.gpu_crash
                                and runtime_options.get("num_gpu") != 0
                                and gpu_crash_retries_left > 0
                            ):
                                gpu_crash_retries_left -= 1
                                if not model_was_preloaded:
                                    quality_mod.ensure_model_unloaded(ollama_tag)
                                runtime_options = dict(runtime_options)
                                runtime_options["num_gpu"] = 0
                                if contribute_mode and enforce_memory_guard:
                                    (
                                        cpu_guard_allowed,
                                        cpu_target_preloaded,
                                        contribution_memory_estimate,
                                        cpu_pressure_threshold_gb,
                                        cpu_guard_reason,
                                    ) = _guard_contribution_load(
                                        ollama_tag,
                                        dest,
                                        runtime_hw,
                                        runtime_profile,
                                        runtime_options,
                                        contribution_memory_estimate,
                                    )
                                    model_was_preloaded = (
                                        model_was_preloaded or cpu_target_preloaded
                                    )
                                    if pressure_watcher is not None:
                                        pressure_watcher.monitor.threshold_gb = (
                                            cpu_pressure_threshold_gb
                                        )
                                    if not cpu_guard_allowed:
                                        guard_failure_reason = (
                                            cpu_guard_reason
                                            or "memory_allocation_blocked"
                                        )
                                        result = None
                                        break
                                already_warned = bool(gpu_state and gpu_state.get("force_cpu"))
                                if gpu_state is not None:
                                    gpu_state["force_cpu"] = True
                                err_console.print(
                                    f"[warning]{filename} crashed the GPU backend - retrying on "
                                    "CPU only.[/warning]"
                                )
                                if not already_warned:
                                    err_console.print(
                                        "[warning]This usually means the GPU driver is too old "
                                        "for this build of Ollama (a CUDA/ROCm 'unsupported "
                                        "toolchain' error). Update your GPU driver to the latest "
                                        "version to restore GPU acceleration; the rest of this "
                                        "session will keep running on CPU only in the "
                                        "meantime.[/warning]"
                                    )
                                continue
                            raise
                except _Interrupted as e:
                    raise InstallInterrupted(filename) from e
                except quality_mod.QualityEvaluationCancelled as e:
                    raise InstallInterrupted(filename) from e
                except quality_mod.QualityEvaluationError as error:
                    result = None
                    eval_error = error
                    err_console.print(
                        f"[warning]Benchmarking {filename} stopped: {error}. "
                        "Cleaning up and moving on.[/warning]"
                    )
                    if contribute_mode:
                        # Queue only - an unattended loop must not block on
                        # an HTTP round trip, and the send policy was
                        # already resolved once at `contribute` start.
                        error_report.queue_report(
                            error,
                            trigger="install_quality_eval",
                            catalog_ref=error_report.catalog_ref(repo_id, filename),
                            engine=benchmark_engine,
                        )
                finally:
                    if not model_was_preloaded:
                        if benchmark_engine == "lmstudio":
                            # No LM Studio equivalent of ensure_model_unloaded's
                            # /api/ps-confirmed unload exists yet (Ollama-only
                            # by design, see quality.py) - best-effort unload,
                            # same as quality._evaluate_tag_once already does
                            # for both engines.
                            if benchmark_tag is not None:
                                quality_mod.unload_model(benchmark_tag, engine="lmstudio")
                        else:
                            quality_mod.ensure_model_unloaded(ollama_tag)
                if result is not None:
                    tokens_per_sec = result["speed"]["median_tokens_per_sec"]
                    samples = result["speed"]["samples_tokens_per_sec"]
                    speed_samples = samples
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
                    engine_version = _engine_version(benchmark_engine)
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
                    raise InstallInterrupted(filename) from e
                finally:
                    if not model_was_preloaded:
                        quality_mod.unload_model(ollama_tag)
        finally:
            if pressure_watcher is not None:
                pressure_watcher.__exit__(None, None, None)
            if started_daemon is not None:
                _stop_engine_daemon(benchmark_engine, started_daemon)

        if pressure_watcher is not None and pressure_watcher.pressure_triggered:
            if benchmark_engine == "ollama":
                pressure_watcher.cancelled = bool(
                    pressure_watcher.cancelled
                    and quality_mod._model_is_loaded(benchmark_tag) is False
                )
            # LM Studio has no /api/ps-equivalent unload-confirmation probe
            # yet (Ollama-only by design, see quality.py) - trust the
            # watcher's own verdict rather than silently downgrading it.
            tokens_per_sec = None
            guard_failure_reason = (
                "memory_pressure_cancelled"
                if pressure_watcher.cancelled
                else "memory_pressure_unload_failed"
            )
            for line in _memory_pressure_report_lines(
                benchmark_engine, pressure_watcher.cancelled
            ):
                err_console.print(line)

        if pressure_watcher is not None:
            memory_measurement = {
                "ram_available_before_gb": pressure_watcher.first_available_gb,
                "ram_available_min_gb": pressure_watcher.minimum_available_gb,
                "ram_available_after_gb": pressure_watcher.last_available_gb,
                "memory_pressure_observed": pressure_watcher.pressure_observed,
            }

        if tokens_per_sec:
            console.print(f"[accent]{tokens_per_sec:.1f} tok/s[/accent]")
            # The dispersion and memory checks below only apply to contribute
            # runs, which record the samples and the pressure window they need.
            # The background-load check applies to every benchmark: a plain
            # install would otherwise fold a load-depressed number straight
            # into the local calibration factor, which is exactly the kind of
            # transient error calibration is supposed to be free of.
            stable_for_calibration = not contribute_mode or (
                memory_measurement is not None
                and memory_measurement.get("memory_pressure_observed") is False
                and speed_samples is not None
                and contribute_memory.speed_mad_ratio(speed_samples) <= 0.15
            )
            if stable_for_calibration and not host_cpu_busy:
                _maybe_auto_calibrate(filename, repo_id, dest, tokens_per_sec)
            elif not stable_for_calibration:
                console.print(
                    "[muted]Local calibration not updated because this measurement "
                    "was pressured or unstable.[/muted]"
                )
            else:
                console.print(
                    "[muted]Local calibration not updated because this measurement "
                    "was taken while other programs were using the CPU.[/muted]"
                )

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
                    engine=benchmark_engine,
                    speed_samples=speed_samples,
                    memory_measurement=memory_measurement,
                    memory_estimate=contribution_memory_estimate,
                    host_cpu_load_percent=host_cpu_load_percent,
                )
            else:
                telemetry.log_attempt("declined_by_user", filename)
        else:
            telemetry_sent = _report_telemetry(
                filename,
                repo_id,
                tokens_per_sec,
                provider=resolved.provider,
                failure_reason=(
                    guard_failure_reason
                    or (eval_error.failure_reason if eval_error is not None else None)
                ),
                engine=benchmark_engine,
            )
        if verify_runtime_after_install:
            compatibility_status = "passed" if tokens_per_sec else "failed"
            _record_install_compatibility(
                filename,
                benchmark_engine,
                status=compatibility_status,
                runtime_version=ollama_runtime_version or engine_version,
                failure_reason=None if tokens_per_sec else (
                    guard_failure_reason
                    or (eval_error.failure_reason if eval_error is not None else "empty_response")
                ),
            )
    elif not runtime_load_declined and selected_runtime is None and not linked["ollama"]:
        telemetry.log_attempt("not_attempted_no_ollama_link", filename)

    return InstallOutcome(
        filename, repo_id, linked, ollama_tag, tokens_per_sec, telemetry_sent, sha256=sha256,
        failure_reason=(
            guard_failure_reason
            or (eval_error.failure_reason if eval_error is not None else None)
        ),
        model_metadata=model_metadata,
        compatibility_engine=selected_runtime,
        compatibility_status=compatibility_status,
        runtime_load_declined=runtime_load_declined,
        benchmark_engine=benchmark_engine if (run_ollama_benchmark or run_lmstudio_benchmark) else None,
    )


def _report_lmstudio_load_verification(outcome: InstallOutcome) -> None:
    """Best-effort proof that a just-linked LM Studio model actually
    loads - LM Studio has no benchmark path to exercise this later the way
    `omm benchmark` does for Ollama. Only a confirmed failure is reported;
    "couldn't check" (lms missing, server unreachable, timeout) stays
    silent, matching the existing Ollama compat-check convention of never
    surfacing an inconclusive result as a warning.

    This is the older `lms`-CLI-based probe. When `_verify_lmstudio_after_install`
    already ran the newer HTTP-adapter probe in this same install
    (`outcome.compatibility_engine == "lmstudio"`), that probe already loaded
    and released the model - running this one too would load/unload it a
    second time for no extra signal, so it's skipped. This older probe still
    has unique value when LM Studio was linked but never selected for
    adapter-based verification (e.g. another runtime was verified instead, or
    `--no-verify-runtime` was used)."""
    if not outcome.linked.get("lmstudio"):
        return
    if outcome.compatibility_engine == "lmstudio":
        return
    result = linker.verify_lmstudio_load(MODELS_DIR / outcome.filename, outcome.repo_id)
    if result is False:
        console.print(
            "[warning]Warning: LM Studio linked this model but it did not "
            "load successfully in a live test.[/warning]"
        )


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
    verify_runtime: bool | None = typer.Option(
        None,
        "--verify-runtime/--no-verify-runtime",
        help="Run (or skip) a short local load/generation check after linking. "
        "Unset asks before loading an unloaded model.",
    ),
) -> None:
    """Download a model into the central hub and link it into installed engines."""
    if not isinstance(verify_runtime, (bool, type(None))):
        verify_runtime = None

    model_name = _resolve_ref(model_name)
    try:
        resolved = _resolve_model_interactive(model_name)
    except ModelResolutionError as e:
        errors.print_cli_error(err_console, str(e), fix=e.fix)
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e

    listener = _EscListener()
    listener.start()
    try:
        outcome = _install_impl(
            resolved,
            skip_unfit=skip_unfit,
            auto_upload=upload is True,
            no_upload=upload is False,
            assume_yes=_global_opts().yes,
            force=force,
            verify_runtime_after_install=True,
            runtime_load_consent=(
                verify_runtime
                if verify_runtime is not None
                else (True if _global_opts().yes else None)
            ),
            preferred_runtime=load_config().get("default_engine"),
            enforce_memory_guard=True,
            stop_event=listener.stop_event,
        )
    except DownloadError as error:
        errors.print_cli_error(err_console, str(error), fix=error.fix)
        raise typer.Exit(1) from error
    except linker.LinkError as error:
        errors.print_cli_error(err_console, str(error), fix=error.fix)
        raise typer.Exit(1) from error
    except InstallInterrupted as e:
        _cleanup_interrupted_install(e.filename)
        err_console.print("[warning]Cancelled.[/warning]")
        raise typer.Exit(0) from e
    except KeyboardInterrupt:
        # Windows Ctrl+C is a console control event, not the Esc listener's
        # stop_event - it can land mid-download, mid-checksum, or mid-link
        # instead of at the _run_interruptible() checkpoints stop_event
        # covers. Route it through the same unload-before-delete cleanup so
        # it doesn't strand a partial GGUF or a linked-but-unregistered file.
        _cleanup_interrupted_install(resolved.filename)
        raise
    finally:
        listener.stop_event.set()

    if outcome.skipped_unfit:
        console.print(
            f"[muted]Skipped {outcome.filename}: predicted not to run on this hardware.[/muted]"
        )
        return
    if outcome.skipped_low_disk:
        # _install_impl already printed the exact volume/capacity reason.
        return

    console.print(f"[success]Ω Installed {outcome.filename}[/success]")
    if outcome.linked.get("ollama"):
        console.print(f"  Ollama: [success]ollama run {outcome.ollama_tag}[/success]")
    for spec in linker.ENGINES:
        if spec.key != "ollama" and outcome.linked.get(spec.key):
            console.print(f"  {spec.label}: visible in your local models list")
    console.print(f"  Uninstall with: [accent]omm uninstall {outcome.filename}[/accent]")
    if any(outcome.linked.get(spec.key) for spec in linker.ENGINES):
        console.print(f"  Run it now: [accent]omm run {outcome.filename}[/accent]")
    _report_lmstudio_load_verification(outcome)


def _cleanup_download_parts(destination: Path) -> bool:
    part = destination.with_suffix(destination.suffix + ".part")
    metadata = part.with_name(f"{part.name}.meta")
    cleaned = False
    for path in (part, _sidecar_path(part), metadata):
        if path.exists():
            _unlink_with_retry(path)
            cleaned = cleaned or not path.exists()
    return cleaned


def _cleanup_incomplete_install(filename: str) -> bool:
    try:
        dest = _managed_model_path(filename)
    except ModelResolutionError:
        return False
    cleaned = _cleanup_download_parts(dest)
    if dest.exists():
        _unlink_with_retry(dest)
        cleaned = cleaned or not dest.exists()
    return cleaned


def _unlink_with_retry(path: Path, *, attempts: int = 8) -> bool:
    """Bounded Windows handle-release retry; return whether the path is gone."""
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if attempt == attempts - 1:
                return not path.exists()
            time.sleep(min(0.1 * (2**attempt), 1.0))
    return not path.exists()


class _PendingOllamaUnlinks:
    """Collects `unlink_ollama` calls made while removing several models in
    one command (`omm uninstall all`) so the expensive final orphaned-blob
    rescan of the manifest tree runs once per models_dir at the end instead
    of once per model (see issue #181)."""

    def __init__(self) -> None:
        self._specs: dict[Path, list[tuple[str, Path | None, str | None]]] = {}

    def add(
        self,
        models_dir: Path,
        model_name: str,
        expected_source: Path | None,
        expected_content_sha256: str | None,
    ) -> None:
        self._specs.setdefault(models_dir, []).append(
            (model_name, expected_source, expected_content_sha256)
        )

    def flush(self) -> None:
        for models_dir, specs in self._specs.items():
            linker.unlink_ollama_batch(specs, models_dir=models_dir)
        self._specs.clear()


def _remove_one(
    filename: str,
    entry: dict,
    *,
    ollama_tag: str | None = None,
    pending_ollama_unlinks: "_PendingOllamaUnlinks | None" = None,
) -> bool:
    try:
        dest = _managed_model_path(filename)
    except ModelResolutionError as error:
        err_console.print(
            f"[warning]Removed unsafe registry entry {filename!r} without touching "
            f"the filesystem ({error}).[/warning]"
        )
        registry.remove_entry(filename)
        return True
    linked = entry.get("linked", {})
    cleared_links: dict[str, bool] = {}
    if ollama_tag is None:
        ollama_tag = linker.resolve_ollama_runtime_name(filename, entry)
    if linked.get("ollama") and benchmark.ollama_daemon_reachable():
        quality_mod.ensure_model_unloaded(ollama_tag, max_wait_seconds=10)
    for spec in linker.ENGINES:
        if linked.get(spec.key):
            try:
                linker.unlink_engine(
                    spec.key,
                    filename,
                    entry,
                    defer_ollama_unlink=(
                        pending_ollama_unlinks.add
                        if pending_ollama_unlinks is not None
                        else None
                    ),
                )
                cleared_links[spec.key] = False
            except linker.LinkError as error:
                err_console.print(
                    f"[warning]{filename}: {spec.label} cleanup skipped: {error}[/warning]"
                )
    # `omm link <directory>` records the exact destination.  It may be a
    # Windows hard link, so use the ownership-aware remover rather than ever
    # unlinking an arbitrary regular file at that path.
    remaining_custom_links: list[str] = []
    for destination in entry.get("custom_links", []):
        if isinstance(destination, str):
            if not linker.unlink_owned_link(Path(destination), expected_source=dest):
                remaining_custom_links.append(destination)

    removed_model = _unlink_with_retry(dest)
    part = dest.with_suffix(dest.suffix + ".part")
    _unlink_with_retry(part)
    _unlink_with_retry(_sidecar_path(part))
    _unlink_with_retry(part.with_name(f"{part.name}.meta"))

    if not removed_model:
        registry.upsert_entry(
            filename,
            linked=cleared_links,
            custom_links=remaining_custom_links,
        )
        err_console.print(
            f"[error]Could not remove {filename}; the registry entry was kept so "
            "you can close the program holding the file and retry.[/error]"
        )
        return False

    registry.remove_entry(filename)
    console.print(f"[success]Removed {filename}[/success]")
    return True


@app.command(name="uninstall")
@global_flags
def remove(
    filename: str = typer.Argument(..., autocompletion=complete_remove_filename),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be uninstalled without removing anything."
    ),
) -> None:
    """Uninstall a model and clean up all symlinks/manifests. Pass `all` to
    uninstall every model installed via omm.

    Alias: rm"""
    if filename.lower() == "all":
        reg = registry.load_registry()
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if dry_run:
            for name in reg:
                console.print(f"Would uninstall: {name}")
            raise typer.Exit(0)
        if not _global_opts().yes and not _ask_confirm(f"Uninstall all {len(reg)} model(s)?"):
            err_console.print("[warning]Cancelled.[/warning]")
            raise typer.Exit(0)
        reg_items = list(reg.items())
        # Resolved once for every entry instead of once per removal below,
        # and their Ollama-manifest deletions batched through
        # pending_ollama_unlinks - both would otherwise re-walk the whole
        # Ollama manifest tree per model removed (see issue #181).
        resolved_ollama_tags = linker.resolve_ollama_runtime_names_batch(reg_items)
        pending_ollama_unlinks = _PendingOllamaUnlinks()
        failed: list[str] = []
        for name, entry in reg_items:
            if not _remove_one(
                name,
                entry,
                ollama_tag=resolved_ollama_tags.get(name),
                pending_ollama_unlinks=pending_ollama_unlinks,
            ):
                failed.append(name)
        pending_ollama_unlinks.flush()
        if failed:
            err_console.print(
                f"[error]{len(failed)} model(s) could not be removed; retry after closing "
                "programs using them.[/error]"
            )
            raise typer.Exit(1)
        return

    filename = _resolve_ref(filename)
    reg = registry.load_registry()
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        dest = MODELS_DIR / filename
        part = dest.with_suffix(dest.suffix + ".part")
        incomplete_install_exists = dest.exists() or part.exists()
        if dry_run:
            if incomplete_install_exists:
                console.print(f"Would clean up incomplete install of {filename}")
                raise typer.Exit(0)
            _print_not_installed_error(filename)
            raise typer.Exit(1)
        if _cleanup_incomplete_install(filename):
            console.print(f"[success]Cleaned up incomplete install of {filename}[/success]")
            raise typer.Exit(0)
        _print_not_installed_error(filename)
        raise typer.Exit(1)

    if dry_run:
        console.print(f"Would uninstall: {filename}")
        raise typer.Exit(0)
    if not _remove_one(filename, entry):
        raise typer.Exit(1)


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


_VERIFY_ENGINES = {"ollama", "lmstudio"}


def _compatibility_adapter(engine: str):
    if engine == "ollama":
        return OllamaAdapter()
    if engine == "lmstudio":
        return LMStudioAdapter()
    raise ValueError(f"unsupported verification engine: {engine}")


def _compatibility_model_ref(filename: str, entry: dict, engine: str) -> RuntimeModelRef:
    if engine == "ollama":
        tag = linker.resolve_ollama_runtime_name(filename, entry)
        link_name = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
        aliases = tuple(
            dict.fromkeys(
                value
                for value in (
                    tag.removesuffix(":latest"),
                    link_name,
                    link_name.removesuffix(":latest"),
                )
                if value and value != tag
            )
        )
        return RuntimeModelRef(tag, aliases)
    repo_id = entry.get("repo_id")
    # LM Studio's real modelKey (lowercase, publisher folder stripped, and
    # any quant suffix shared with the GGUF's own quant stripped too - see
    # linker._lmstudio_model_key's docstring) is never the repo_id/filename
    # guess below. resolve_lmstudio_model() gets it right by matching the
    # on-disk path against `lms ls --json`, same as the install-time
    # benchmark path already does; only fall back to guessing when `lms`
    # itself is unavailable or the model isn't visible yet.
    resolved = linker.resolve_lmstudio_model(repo_id, filename)
    model_key = resolved.get("model_key") if resolved else None
    key = model_key or (
        repo_id if isinstance(repo_id, str) and "/" in repo_id else f"local/{Path(filename).stem}"
    )
    aliases = tuple(
        dict.fromkeys(
            value
            for value in (repo_id, filename, Path(filename).stem)
            if isinstance(value, str) and value and value != key
        )
    )
    return RuntimeModelRef(key, aliases)


def _select_compatibility_engine(entry: dict, requested: str | None) -> str:
    linked = entry.get("linked") if isinstance(entry.get("linked"), dict) else {}
    available = [engine for engine in ("ollama", "lmstudio") if linked.get(engine)]
    if requested is not None:
        requested = requested.casefold()
        if requested not in _VERIFY_ENGINES:
            raise ValueError("--engine must be ollama or lmstudio")
        if requested not in available:
            raise ValueError(f"the model is not linked to {requested}")
        return requested
    if not available:
        raise ValueError("the model is not linked to Ollama or LM Studio")
    configured = load_config().get("default_engine")
    if configured in available:
        return configured
    reachable = [
        engine for engine in available if _compatibility_adapter(engine).health().reachable
    ]
    if len(reachable) == 1:
        return reachable[0]
    choices = reachable or available
    if len(choices) == 1:
        return choices[0]
    import questionary

    selected = _ask_select(
        questionary.select(
            "Runtime to verify:",
            choices=[
                questionary.Choice("Ollama" if value == "ollama" else "LM Studio", value=value)
                for value in choices
            ],
        )
    )
    if selected is None:
        raise typer.Abort()
    return selected


_COMPATIBILITY_FAILURE_MESSAGES = {
    "server_unavailable": "the local server is not running or reachable",
    "model_not_visible": "the linked model is not visible to the runtime",
    "load_failed": "the runtime could not load the model",
    "out_of_memory": "there was not enough memory to load or run the model",
    "generation_timeout": "the short test response timed out",
    "empty_response": "the runtime returned an empty response",
    "unload_failed": "OMM could not confirm that its test load was released",
    "unsupported_runtime": "this runtime version or model type is unsupported",
    "unknown": "the runtime returned an unrecognized error; check its server settings and LM_API_TOKEN if authentication is enabled",
}


@app.command()
@marks_command_body_ran
def verify(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
    engine: str = typer.Option(
        None,
        "--engine",
        help="Local runtime to test: ollama or lmstudio.",
    ),
    keep_loaded: bool = typer.Option(
        False,
        "--keep-loaded",
        help="Keep a model loaded only when this command loaded it.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Load the model without asking. For scripting.",
    ),
) -> None:
    """Prove that an installed model can load and return local text.

    If the selected engine's daemon isn't running, this starts it (asking
    first unless --yes) and stops it again afterward, unless --keep-loaded
    left the model loaded on it.
    """
    model_name = _resolve_ref(model_name)
    filename, entry = _lookup_entry(model_name, registry.load_registry())
    if entry is None:
        _print_not_installed_error(model_name)
        raise typer.Exit(1)
    try:
        selected_engine = _select_compatibility_engine(entry, engine)
    except ValueError as error:
        err_console.print(f"[error]{error}.[/error]")
        raise typer.Exit(1) from error

    daemon_handle = None
    try:
        adapter = _compatibility_adapter(selected_engine)
        model_ref = _compatibility_model_ref(filename, entry, selected_engine)
        health = adapter.health()
        if (
            selected_engine == "ollama"
            and not health.reachable
            and benchmark.ollama_install_state() == "stopped"
        ):
            # Installed but not running - offer to start it, same
            # start/ask/stop-when-done pattern install uses.
            try:
                daemon_handle = _ensure_ollama_running("verify", assume_yes=yes)
            except typer.Exit:
                daemon_handle = None
            if daemon_handle is not None:
                console.print("[muted]Started Ollama in the background for this verification.[/muted]")
                health = adapter.health()
        elif (
            selected_engine == "lmstudio"
            and not health.reachable
            and linker._lms_cli_path() is not None
        ):
            # Same pattern as the Ollama branch above.
            try:
                daemon_handle = _ensure_lmstudio_running("verify", assume_yes=yes)
            except typer.Exit:
                daemon_handle = None
            if daemon_handle is not None:
                console.print(
                    "[muted]Started LM Studio's local server for this verification.[/muted]"
                )
                health = adapter.health()
        visible = None
        if health.reachable:
            try:
                visible = find_runtime_model(adapter.list_models(), model_ref)
            except RuntimeAdapterError:
                visible = None
            if (visible is None or not visible.loaded) and not yes:
                label = _engine_label(selected_engine)
                if not _ask_confirm(
                    f"Load {filename} into {label} memory for a short local test?"
                ):
                    err_console.print("[warning]Verification cancelled; nothing was loaded.[/warning]")
                    raise typer.Exit(0)

        if health.reachable and (visible is None or not visible.loaded):
            size_bytes = entry.get("size_bytes")
            if not _positive_finite_number(size_bytes):
                try:
                    size_bytes = _managed_model_path(filename).stat().st_size
                except (ModelResolutionError, OSError):
                    size_bytes = None
            if not _positive_finite_number(size_bytes):
                label = _engine_label(selected_engine)
                err_console.print(
                    f"[error]Memory Guard could not determine the model size; "
                    f"the {label} load was blocked.[/error]"
                )
                raise typer.Exit(1)
            guard_allowed, _runtime, _preloaded = _guard_engine_load(
                selected_engine,
                model_ref.key,
                float(size_bytes) / (1024**3) * 1.2,
            )
            if not guard_allowed:
                raise typer.Exit(1)

        console.print(f"Verifying {filename} with {_engine_label(selected_engine)}...")
        result = verify_and_record(
            filename,
            adapter,
            model_ref,
            keep_loaded=keep_loaded,
        )
        if result.status == "passed":
            detail = "already loaded and preserved" if result.model_was_preloaded else (
                "left loaded as requested" if result.model_left_loaded else "test load released"
            )
            console.print(f"[success]Compatible: local text generation succeeded ({detail}).[/success]")
            if result.model_left_loaded:
                daemon_handle = None
            return
        reason = result.failure_reason or "unknown"
        err_console.print(
            f"[error]Compatibility check failed: {_COMPATIBILITY_FAILURE_MESSAGES.get(reason, reason)}.[/error]"
        )
        err_console.print("[muted]The downloaded model was kept; no model file was deleted.[/muted]")
        raise typer.Exit(1)
    finally:
        if daemon_handle is not None:
            _stop_engine_daemon(selected_engine, daemon_handle)


@app.command()
@global_flags
def info(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
    """Show name, version, size, and linked-program run commands for an installed model."""
    json_output = _global_opts().json
    model_name = _resolve_ref(model_name)
    reg = registry.load_registry()
    filename, entry = _lookup_entry(model_name, reg)
    if entry is None:
        _print_not_installed_error(model_name)
        raise typer.Exit(1)

    stored_size = entry.get("size_bytes")
    size_bytes = stored_size if _positive_finite_number(stored_size) else 0
    size_gb = size_bytes / (1024**3)
    linked = entry.get("linked", {})

    ollama_tag = linker.resolve_ollama_runtime_name(filename, entry)

    if json_output:
        console.print_json(
            data={
                "filename": filename,
                "repo_id": entry.get("repo_id"),
                "provider": entry.get("provider") or ("huggingface" if entry.get("repo_id") else None),
                "version": _entry_version(entry),
                "size_bytes": size_bytes,
                "installed_at": entry.get("installed_at", "unknown"),
                "linked": {spec.key: bool(linked.get(spec.key)) for spec in linker.ENGINES},
                "ollama_run_command": f"ollama run {ollama_tag}" if linked.get("ollama") else None,
                "compatibility": entry.get("compatibility", {}),
            }
        )
        return

    table = _table(title=filename, show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    repo_label = entry.get("repo_id") or "(direct URL install)"
    provider = entry.get("provider")
    if entry.get("repo_id") and provider and provider != "huggingface":
        repo_label = f"{repo_label} [{provider}]"
    table.add_row("Repo", repo_label)
    table.add_row("Version", _entry_version(entry))
    table.add_row("Size", f"{size_gb:.2f} GB")
    table.add_row("Installed at", entry.get("installed_at", "unknown"))
    compatibility = entry.get("compatibility")
    if isinstance(compatibility, dict):
        for engine_key in ("ollama", "lmstudio"):
            result = compatibility.get(engine_key)
            if isinstance(result, dict):
                status = result.get("status", "unknown")
                reason = result.get("failure_reason")
                table.add_row(
                    f"{engine_key} verification",
                    f"{status} ({reason})" if reason else str(status),
                )
    installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
    for spec in linker.ENGINES:
        if not installed[spec.key]:
            continue
        if spec.key == "ollama":
            table.add_row("Ollama", f"ollama run {ollama_tag}" if linked.get("ollama") else "not linked")
        else:
            table.add_row(
                spec.label,
                f"linked (visible in {spec.label})" if linked.get(spec.key) else "not linked",
            )

    console.print(table)
    if size_bytes > 0:
        console.print()
        console.print(_fit_card(filename, int(size_bytes)))
    note = _missing_engines_note(installed)
    if note:
        console.print(note, style="muted")


def _update_one(filename: str, entry: dict) -> str:
    """Refresh one installed model against its source. Returns "updated",
    "up_to_date", or "skipped". HF-repo installs check a cheap remote hash
    first and only re-download on a mismatch; direct-URL installs have no
    such endpoint, so they re-download to a temp file and compare hashes
    before swapping it in."""
    try:
        filename = validate_model_filename(filename)
    except ModelResolutionError as error:
        err_console.print(f"[error]{filename}: unsafe registry filename ({error}).[/error]")
        return "skipped"
    try:
        dest = _managed_model_path(filename)
    except ModelResolutionError as error:
        err_console.print(f"[error]{filename}: unsafe registry filename ({error}).[/error]")
        return "skipped"
    repo_id = entry.get("repo_id")
    try:
        provider = validate_provider(entry.get("provider") or "huggingface")
    except ModelResolutionError as error:
        err_console.print(f"[error]{filename}: unsafe registry provider ({error}).[/error]")
        return "skipped"
    old_sha256 = entry.get("sha256")
    tmp = dest.with_name(dest.name + ".update")

    if repo_id:
        try:
            repo_id = validate_repo_id(repo_id)
        except ModelResolutionError as error:
            err_console.print(f"[error]{filename}: unsafe repository id ({error}).[/error]")
            return "skipped"
        remote_sha256 = remote_file_sha256(provider, repo_id, filename)
        if remote_sha256 is None:
            err_console.print(
                f"[warning]{filename}: could not check for updates "
                "(no repo/LFS info), skipped.[/warning]"
            )
            return "skipped"
        if (
            remote_sha256 == old_sha256
            and dest.is_file()
            and sha256_file(dest) == remote_sha256
        ):
            return "up_to_date"

        url = download_url(provider, repo_id, filename)
        opts = _global_opts()
        try:
            download_file(url, tmp, quiet=opts.quiet, no_color=opts.no_color)
        except DownloadError as e:
            err_console.print(f"[error]{filename}: update download failed: {e}[/error]")
            tmp.unlink(missing_ok=True)
            _cleanup_download_parts(tmp)
            return "skipped"
        new_sha256 = sha256_file(tmp)
        if new_sha256 != remote_sha256:
            err_console.print(
                f"[error]{filename}: downloaded SHA-256 does not match provider metadata; "
                "the installed file was preserved.[/error]"
            )
            tmp.unlink(missing_ok=True)
            return "skipped"
    else:
        source = entry.get("source")
        if not source:
            err_console.print(f"[warning]{filename}: no source URL on record, skipped.[/warning]")
            return "skipped"

        tmp = dest.with_name(dest.name + ".update")
        opts = _global_opts()
        try:
            download_file(source, tmp, quiet=opts.quiet, no_color=opts.no_color)
        except DownloadError as e:
            err_console.print(f"[error]{filename}: update download failed: {e}[/error]")
            tmp.unlink(missing_ok=True)
            _cleanup_download_parts(tmp)
            return "skipped"

        new_sha256 = sha256_file(tmp)
        if (
            new_sha256 == old_sha256
            and dest.is_file()
            and sha256_file(dest) == new_sha256
        ):
            tmp.unlink(missing_ok=True)
            return "up_to_date"
    try:
        _ensure_install_disk_capacity(
            dest,
            tmp.stat().st_size,
            include_download=False,
            only_engine=None,
        )
    except (InsufficientDiskSpaceError, OSError) as error:
        err_console.print(
            f"[error]{filename}: update cannot be linked safely: {error}. "
            "The installed file was preserved.[/error]"
        )
        tmp.unlink(missing_ok=True)
        _cleanup_download_parts(tmp)
        return "skipped"

    try:
        tmp.replace(dest)
    except OSError as e:
        err_console.print(f"[error]{filename}: update failed to finalize: {e}[/error]")
        tmp.unlink(missing_ok=True)
        return "skipped"

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


def _pick_run_engine(entry: dict, requested: str | None) -> str:
    """Which runner `omm run` should start for this registry entry.

    `requested` (the --engine flag) wins if the model is linked there;
    otherwise the configured `default_engine`, otherwise the first
    linked-and-installed runner in launcher.ENGINE_PRIORITY. Exits with a
    readable message instead of guessing when nothing qualifies."""
    known = {spec.key for spec in linker.ENGINES}
    linked = entry.get("linked", {}) or {}
    if requested is not None:
        if requested not in known:
            err_console.print(
                f"[error]Unknown engine '{requested}'. Choose from: {', '.join(sorted(known))}.[/error]"
            )
            raise typer.Exit(2)
        if not linked.get(requested):
            err_console.print(
                f"[error]This model is not linked into {_engine_label(requested)}. "
                f"Run `omm link --engine {requested}` first.[/error]"
            )
            raise typer.Exit(1)
        return requested

    candidates = [
        key for key in launcher.ENGINE_PRIORITY
        if linked.get(key) and linker.is_engine_installed(key)
    ]
    configured = load_config().get("default_engine")
    if configured in candidates:
        return configured
    if candidates:
        return candidates[0]
    err_console.print(
        "[error]This model is not linked into any installed runner. "
        "Run `omm link` to repair links, or `omm setup` to install a runner.[/error]"
    )
    raise typer.Exit(1)


def _pick_run_model(reg: dict) -> str:
    """`omm run` with no model name: use the only installed model, or ask
    when there are several. Non-TTY callers must name the model."""
    names = sorted(reg)
    if not names:
        err_console.print(
            "[error]No models installed yet. Try `omm recommend` to pick one that fits this PC.[/error]"
        )
        raise typer.Exit(1)
    if len(names) == 1:
        return names[0]
    if not _stdin_is_tty():
        err_console.print(
            "[error]Several models are installed; name one: `omm run <model>` (see `omm list`).[/error]"
        )
        raise typer.Exit(1)
    import questionary

    choice = _add_escape_to_cancel(
        questionary.select("Which model do you want to run?", choices=names)
    ).ask()
    if choice is None:
        raise typer.Abort()
    return choice


@app.command()
@global_flags
def run(
    model_name: str = typer.Argument(
        None, autocompletion=complete_remove_filename, help="Installed model (see `omm list`)."
    ),
    engine: str = typer.Option(
        None, "--engine", "-e",
        help="Runner to use (ollama, lmstudio, jan, koboldcpp, textgenwebui, anythingllm, mstystudio).",
    ),
) -> None:
    """Start a chat with an installed model - Ollama chats right here in the terminal,
    KoboldCpp/text-generation-webui start with the model loaded, GUI apps are opened."""
    reg = registry.load_registry()
    if model_name is None:
        model_name = _pick_run_model(reg)
    model_name = _resolve_ref(model_name)
    filename, entry = _lookup_entry(model_name, reg)
    if entry is None:
        _print_not_installed_error(model_name)
        raise typer.Exit(1)

    chosen = _pick_run_engine(entry, engine)
    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    console.print(
        f"[accent]{filename}[/accent] via [bold]{_engine_label(chosen)}[/bold] "
        f"[muted]({launcher.launch_description(chosen)})[/muted]"
    )

    daemon_handle = None
    if chosen == "ollama":
        daemon_handle = _ensure_ollama_running("run", assume_yes=_global_opts().yes)
        if daemon_handle is not None:
            console.print("[muted]Started Ollama in the background for this chat.[/muted]")
        console.print("[muted]Type /bye to leave the chat.[/muted]")
    try:
        result = launcher.launch(
            chosen,
            model_filename=filename,
            model_path=MODELS_DIR / filename,
            ollama_tag=ollama_tag,
        )
    finally:
        if daemon_handle is not None:
            _stop_engine_daemon("ollama", daemon_handle)
    if not result.ok:
        err_console.print(f"[error]{result.message}[/error]")
        raise typer.Exit(1)
    console.print(f"[success]{result.message}[/success]")


def _fit_card(label: str, size_bytes: int):
    """The omm.run memory card for one model on this PC."""
    hw = scan_hardware()
    budget = calculate_memory_budget(hw)
    size_gb = size_bytes / (1024**3)
    required_gb = predictor.estimate_required_memory_gb({"size_bytes": size_bytes}) or size_gb
    return fit_ui.render_fit(
        hw=hw,
        budget=budget,
        model_label=label,
        size_gb=size_gb,
        required_gb=required_gb,
        width=console.size.width,
    )


@app.command()
@global_flags
def fit(
    model_name: str = typer.Argument(
        ..., autocompletion=complete_install_name, help="Installed model, curated id, repo/file, or search number."
    ),
) -> None:
    """Show whether a model fits this PC's memory right now - installed or not - as a bar
    over what other apps use, the OS reserve, and the install cap."""
    model_name = _resolve_ref(model_name)
    reg = registry.load_registry()
    filename, entry = _lookup_entry(model_name, reg)
    if entry is not None and _positive_finite_number(entry.get("size_bytes")):
        label, size_bytes = filename, int(entry["size_bytes"])
    else:
        try:
            resolved = _resolve_model_interactive(model_name)
        except ModelResolutionError as error:
            err_console.print(f"[error]{error}[/error]")
            raise typer.Exit(1) from error
        size_bytes = None
        if resolved.repo_id and resolved.provider:
            size_bytes = remote_file_size(resolved.provider, resolved.repo_id, resolved.filename)
        if not size_bytes:
            err_console.print(
                f"[error]Could not determine the size of {resolved.filename} "
                "(not installed, and the provider did not report a file size).[/error]"
            )
            raise typer.Exit(1)
        label = resolved.filename
    if _global_opts().json:
        hw = scan_hardware()
        budget = calculate_memory_budget(hw)
        size_gb = size_bytes / (1024**3)
        required_gb = predictor.estimate_required_memory_gb({"size_bytes": size_bytes}) or size_gb
        v = fit_ui.verdict(required_gb, budget)
        console.print_json(
            data={
                "model": label,
                "size_gb": round(size_gb, 2),
                "required_gb": round(required_gb, 2),
                "in_use_gb": round(max(0.0, hw.ram_total_gb - hw.ram_available_gb), 2),
                "reserved_gb": round(budget.ram_safety_reserve_gb, 2),
                "model_budget_gb": round(budget.model_budget_gb, 2),
                "install_cap_gb": round(budget.install_budget_gb, 2),
                "status": v.status,
                "message": v.message,
            }
        )
        return
    console.print(_fit_card(label, size_bytes))


@app.command()
@global_flags
def upgrade(
    model_name: str = typer.Argument(None, autocompletion=complete_remove_filename),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be checked for updates without downloading anything."
    ),
) -> None:
    """Refresh an installed model against its source, re-downloading only
    if the source has changed since install. With no argument (or `all`),
    checks every model installed via omm.

    Alias: up"""
    reg = registry.load_registry()

    if model_name is None or model_name.lower() == "all":
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if dry_run:
            for filename in reg:
                console.print(f"Would check for updates: {filename}")
            raise typer.Exit(0)
        if not _global_opts().yes and not _ask_confirm(f"Check {len(reg)} model(s) for updates?"):
            err_console.print("[warning]Cancelled.[/warning]")
            raise typer.Exit(0)

        counts = {"updated": 0, "up_to_date": 0, "skipped": 0}
        for filename, entry in list(reg.items()):
            counts[_update_one(filename, entry)] += 1
        console.print(
            f"[success]{counts['updated']} updated, {counts['up_to_date']} up to date, "
            f"{counts['skipped']} skipped.[/success]"
        )
        return

    resolved = _resolve_ref(model_name)
    filename, entry = _lookup_entry(resolved, reg)
    if entry is None:
        _print_not_installed_error(resolved)
        raise typer.Exit(1)

    if dry_run:
        console.print(f"Would check for updates: {filename}")
        raise typer.Exit(0)
    result = _update_one(filename, entry)
    if result == "up_to_date":
        console.print(f"[success]{filename} is already up to date ({_entry_version(entry)}).[/success]")
    elif result == "updated":
        fresh_entry = registry.load_registry()[filename]
        console.print(f"[success]{filename} updated to {_entry_version(fresh_entry)}.[/success]")


@app.command(name="list")
@global_flags
def list_models(
    engine: str | None = typer.Option(
        None, "--engine", help="Only show models linked into this engine."
    ),
) -> None:
    """Show models installed via omm and their linked status.

    Alias: ls"""
    _validate_engine(engine)
    json_output = _global_opts().json
    reg = registry.load_registry()
    had_any_models = bool(reg)
    if engine is not None:
        reg = {
            filename: entry
            for filename, entry in reg.items()
            if entry.get("linked", {}).get(engine)
        }
    if not reg:
        if json_output:
            console.print_json(data=[])
        elif engine is not None and had_any_models:
            console.print(
                f"No models linked into {_engine_label(engine)} yet. "
                f"Try `omm link --engine {engine}`."
            )
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

    table = _table(title="omm models")
    table.add_column("#", justify="right")
    table.add_column("Filename", style="accent")
    table.add_column("Size", justify="right")
    table.add_column("Links")

    for idx, (filename, entry) in enumerate(reg.items(), start=1):
        size_gb = entry.get("size_bytes", 0) / (1024**3)
        linked = entry.get("linked", {})
        programs = [spec.label for spec in linker.ENGINES if linked.get(spec.key)]
        table.add_row(
            str(idx), filename, f"{size_gb:.2f} GB", ", ".join(programs) or "none"
        )
    console.print(table)
    session_cache.record_results(list(reg.keys()))


@setting_app.command(name="telemetry")
@global_flags
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
            err_console.print("[error]Use HTTPS, or HTTP only for localhost.[/error]")
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
    table = _table(title="Telemetry destination", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Backend", str(current.get("telemetry_backend") or "local"))
    table.add_row("Endpoint", str(current.get("telemetry_endpoint") or "not configured"))
    console.print(table)


@setting_app.command(name="upload")
@global_flags
def configure_upload(
    enable: bool = typer.Option(False, "--enable", help="Always send benchmark results without asking."),
    disable: bool = typer.Option(False, "--disable", help="Never send benchmark results."),
    ask: bool = typer.Option(
        False,
        "--ask",
        help=(
            "Ask before sending (default); `omm install`/`omm benchmark` ask "
            "each time, `omm contribute` asks once per run."
        ),
    ),
) -> None:
    """Configure the benchmark-upload send policy; see `omm setting telemetry` for the destination."""
    chosen = [flag for flag in (enable, disable, ask) if flag]
    if len(chosen) > 1:
        err_console.print("[error]Choose only one of --enable, --disable, or --ask.[/error]")
        raise typer.Exit(1)
    current = load_config()
    changes = {}
    if enable:
        if not current.get("telemetry_endpoint"):
            err_console.print("[error]Set an endpoint with `omm setting telemetry --endpoint` before enabling uploads.[/error]")
            raise typer.Exit(1)
        changes["telemetry_send_policy"] = "always"
    elif disable:
        changes["telemetry_send_policy"] = "never"
    elif ask:
        changes["telemetry_send_policy"] = "ask"
    if changes:
        current = config_mod.update_config(**changes)
    table = _table(title="Benchmark upload policy", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    policy = current.get("telemetry_send_policy", "ask")
    table.add_row("Uploads", {"always": "always", "never": "never", "ask": "ask (default)"}[policy])
    console.print(table)


@setting_app.command(name="error-reports")
@global_flags
def configure_error_reports(
    enable: bool = typer.Option(False, "--enable", help="Always send scrubbed error reports without asking."),
    disable: bool = typer.Option(False, "--disable", help="Never send error reports (the default)."),
    ask: bool = typer.Option(False, "--ask", help="Ask once per `omm contribute` run before sending."),
) -> None:
    """Configure the opt-in error-report policy; see docs/error-reports.md for what is sent.

    Kept separate from `omm setting upload` on purpose: error reports go to
    a different, write-only channel than benchmark telemetry, so the two
    consents are managed independently.
    """
    chosen = [flag for flag in (enable, disable, ask) if flag]
    if len(chosen) > 1:
        err_console.print("[error]Choose only one of --enable, --disable, or --ask.[/error]")
        raise typer.Exit(1)
    current = load_config()
    changes = {}
    if enable:
        if not error_report.enabled(current):
            err_console.print(
                "[error]Error reports are derived from the telemetry endpoint. Set one with "
                "`omm setting telemetry --endpoint` before enabling them.[/error]"
            )
            raise typer.Exit(1)
        changes["error_report_send_policy"] = "always"
    elif disable:
        changes["error_report_send_policy"] = "never"
    elif ask:
        changes["error_report_send_policy"] = "ask"
    if changes:
        current = config_mod.update_config(**changes)
    if disable:
        # Opting out also drops whatever was queued under an earlier consent
        # - leaving it on disk waiting for a policy that will never come
        # helps nobody.
        discarded = error_report.discard_pending()
        if discarded:
            console.print(f"[muted]Discarded {discarded} queued error report(s).[/muted]")
    table = _table(title="Error report policy", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    labels = {
        "always": "always",
        "never": "never (default)" if not error_report.policy_is_set(current) else "never",
        "ask": "ask once per contribute run",
    }
    table.add_row("Error reports", labels[error_report.send_policy(current)])
    table.add_row("Destination", str(error_report.endpoint(current) or "not available"))
    console.print(table)


@setting_app.command(name="memory-guard")
@global_flags
def configure_memory_guard(
    policy: str = typer.Option(
        None,
        "--policy",
        help="Memory Guard policy: ask, block, or observe.",
    ),
    poll_seconds: float = typer.Option(
        None,
        "--poll-seconds",
        min=0.1,
        max=60.0,
        help="Seconds between live-memory checks during a long operation.",
    ),
    low_memory_seconds: float = typer.Option(
        None,
        "--low-memory-seconds",
        min=0.0,
        max=300.0,
        help="How long low memory must persist before OMM cancels its own operation.",
    ),
) -> None:
    """Show or change the consent-aware runtime memory protection policy."""
    changes = {}
    if policy is not None:
        normalized = policy.casefold()
        if normalized not in {"ask", "block", "observe"}:
            err_console.print("[error]--policy must be ask, block, or observe.[/error]")
            raise typer.Exit(1)
        changes["memory_guard_policy"] = normalized
    if poll_seconds is not None:
        changes["memory_guard_poll_seconds"] = poll_seconds
    if low_memory_seconds is not None:
        changes["memory_guard_low_memory_seconds"] = low_memory_seconds
    current = config_mod.update_config(**changes) if changes else load_config()
    table = _table(title="Memory Guard", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Policy", str(current["memory_guard_policy"]))
    table.add_row("Poll interval", f"{current['memory_guard_poll_seconds']} seconds")
    table.add_row(
        "Sustained pressure",
        f"{current['memory_guard_low_memory_seconds']} seconds",
    )
    console.print(table)


@setting_app.command(name="version")
@global_flags
def configure_version(
    stable: bool = typer.Option(False, "--stable", help="Track the stable channel (main branch)."),
    beta: bool = typer.Option(False, "--beta", help="Track the beta channel (beta branch)."),
) -> None:
    """Show or switch the update channel `omm update` pulls from. Switching
    takes effect immediately - it fetches and checks out the new branch
    right away, no separate `omm update` needed."""
    if stable and beta:
        err_console.print("[error]Choose only one of --stable or --beta.[/error]")
        raise typer.Exit(1)
    requested = "beta" if beta else ("stable" if stable else None)
    current = load_config()
    source = package_metadata.install_source()
    if source is not package_metadata.InstallSource.GIT:
        if requested == "beta":
            err_console.print(
                "[error]The beta channel requires a Git source installation. "
                "This package-managed installation was left unchanged.[/error]\n"
                f"{_package_managed_update_guidance(source, 'omm setting version --beta')}"
            )
            raise typer.Exit(1)
        if requested == "stable" and current.get("update_channel") == "beta":
            current = config_mod.update_config(update_channel="stable")
            console.print("[success]Using stable package-managed releases.[/success]")
        commit = _installed_commit()
        table = _table(title="Update channel", show_header=False)
        table.add_column("Field", style="label")
        table.add_column("Value")
        table.add_row("Channel", "stable (package-managed)")
        table.add_row("Commit", commit[:7] if commit else "unknown")
        console.print(table)
        return
    if requested and requested != (current.get("update_channel") or "stable"):
        branch = _channel_branch(requested)
        result = _perform_update(branch)
        if result.returncode != 0:
            err_console.print(f"[error]Channel switch failed:[/error]\n{result.stderr}")
            raise typer.Exit(1)
        current = config_mod.update_config(update_channel=requested)
        latest = _remote_head_commit(branch)
        if latest:
            version_check.record(latest, branch)
        console.print(f"[success]Switched to the {requested} channel.[/success]")
        _refresh_data()
    channel = current.get("update_channel") or "stable"
    commit = _installed_commit()
    table = _table(title="Update channel", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Channel", f"{channel} ({_channel_branch(channel)})")
    table.add_row("Commit", commit[:7] if commit else "unknown")
    console.print(table)


def _pick_theme_interactively(current_name: str, allow_back: bool = False) -> str | None:
    """Live arrow-key picker with a real-color preview that redraws as the
    highlight moves - see `theme.run_picker`. Returns the pick, or None if
    cancelled (Escape/Ctrl+C) or "← Back".

    This is the only way an existing install ever sees the previews:
    anyone who upgraded into this feature already has
    `onboarding_completed = True`, so the setup wizard's picker never runs
    for them."""
    _require_tty("This selection")
    return theme_mod.run_picker(current_name, current_label="current", allow_back=allow_back)


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
    if set_name is None and _stdin_is_tty():
        # Bare interactive invocation: same preview-and-pick UX as the
        # setup wizard. Without a TTY (scripts, pipes) fall through to the
        # read-only table instead of hanging on a prompt.
        set_name = _pick_theme_interactively(str(current.get("theme", "dark")))
    if set_name is not None:
        if set_name not in theme_mod.THEME_NAMES:
            err_console.print(
                f"[error]--set must be one of: {', '.join(theme_mod.THEME_NAMES)}.[/error]"
            )
            raise typer.Exit(1)
        current = config_mod.update_config(theme=set_name)
        theme_mod.apply_theme_to_console(console, set_name)
        theme_mod.apply_theme_to_console(err_console, set_name)
    table = _table(title="Color theme", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Theme", str(current.get("theme", "dark")))
    console.print(table)


@setting_app.command(name="calibrate")
@global_flags
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
        err_console.print("[error]No Ollama-linked omm models are installed.[/error]")
        raise typer.Exit(1)
    if model_name is None:
        filename, entry = min(eligible, key=lambda item: item[1].get("size_bytes") or 2**63)
    else:
        resolved = _resolve_ref(model_name)
        filename, entry = _lookup_entry(resolved, reg)
        if entry is None or not (entry.get("linked") or {}).get("ollama"):
            err_console.print(f"[error]{resolved} is not linked to Ollama.[/error]")
            raise typer.Exit(1)

    artifact = predictor.load_cached_model()
    if not artifact or not artifact.get("trees"):
        err_console.print("[error]No cached recommendation model is available.[/error]")
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
        err_console.print("[error]This model has no usable baseline speed prediction.[/error]")
        raise typer.Exit(1)
    tag = linker.resolve_ollama_runtime_name(filename, entry)
    # Warn but do not refuse: this measurement was asked for explicitly, and
    # the caller is the one who can decide whether to close things and retry.
    # The automatic post-install calibration, which nobody asked for, does
    # skip itself instead.
    _background_cpu_load_is_high()
    measured = benchmark.benchmark_ollama(tag)
    if measured is None or measured <= 0:
        err_console.print("[error]Calibration requires a running Ollama model server.[/error]")
        raise typer.Exit(1)
    try:
        factor = calibration.record_calibration(
            hardware,
            measured_tokens_per_sec=measured,
            predicted_tokens_per_sec=predicted,
            engine="ollama",
        )
    except OSError as e:
        err_console.print(f"[error]Could not save calibration: {e}[/error]")
        raise typer.Exit(1) from e
    console.print(
        f"[success]Local calibration saved: {measured:.1f} tok/s measured, "
        f"{predicted:.1f} predicted, correction ×{factor:.2f}.[/success]"
    )
    console.print("[muted]The calibration stays in ~/.omm and was not uploaded.[/muted]")


@setting_app.command(name="catalog-trust")
@global_flags
def catalog_trust(
    manifest_url: str = typer.Option(..., "--manifest-url", help="HTTPS manifest URL."),
    public_key: str = typer.Option(..., "--public-key", help="Base64 Ed25519 public key."),
) -> None:
    """Require future recommendation downloads to pass signature verification."""
    if not manifest_url.startswith("https://"):
        err_console.print("[error]The signed catalog manifest must use HTTPS.[/error]")
        raise typer.Exit(1)
    try:
        fingerprint = catalog.public_key_fingerprint(public_key)
    except catalog.CatalogVerificationError as error:
        err_console.print(f"[error]{error}[/error]")
        raise typer.Exit(1) from error
    config_mod.update_config(
        catalog_manifest_url=manifest_url,
        catalog_public_key=public_key,
    )
    console.print(f"[success]Signed catalog verification enabled (key {fingerprint}).[/success]")


@setting_app.command(name="catalog-status")
@global_flags
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
    table = _table(title="Recommendation catalog", show_header=False)
    table.add_column("Field", style="label")
    table.add_column("Value")
    table.add_row("Signed manifest", str(current.get("catalog_manifest_url") or "not configured"))
    table.add_row("Trusted key", fingerprint)
    table.add_row("Rollback snapshots", str(len(catalog.snapshots())))
    console.print(table)


@setting_app.command(name="catalog-rollback")
@global_flags
def catalog_rollback() -> None:
    """Restore the most recent different recommendation snapshot."""
    try:
        current = load_config()
        selected = catalog.rollback(
            require_signed=bool(
                current.get("catalog_manifest_url") and current.get("catalog_public_key")
            )
        )
    except (OSError, ValueError) as error:
        err_console.print(f"[error]Catalog rollback failed: {error}[/error]")
        raise typer.Exit(1) from error
    console.print(f"[success]Rolled back recommendation catalog from {selected.name}.[/success]")


@setting_app.callback(invoke_without_command=True)
def setting_menu(ctx: typer.Context) -> None:
    """Bare `omm setting` opens an interactive menu; a subcommand skips it."""
    import questionary

    if ctx.invoked_subcommand is not None:
        return
    while True:
        current = load_config()
        telemetry_backend = current.get("telemetry_backend") or "local"
        telemetry_endpoint = current.get("telemetry_endpoint") or "not configured"
        upload_policy = current.get("telemetry_send_policy", "ask")
        catalog_manifest = current.get("catalog_manifest_url") or "not configured"
        update_channel = current.get("update_channel") or "stable"
        memory_guard_policy = current.get("memory_guard_policy", "ask")
        error_reports_policy = error_report.send_policy(current)

        choice = _ask_select(
            questionary.select(
                "What do you want to change?",
                choices=[
                    questionary.Choice(
                        f"Telemetry (current: {telemetry_backend}, {telemetry_endpoint})",
                        value="telemetry",
                    ),
                    questionary.Choice(f"Upload (current: {upload_policy})", value="upload"),
                    questionary.Choice(f"Version channel (current: {update_channel})", value="version"),
                    questionary.Choice(
                        f"Theme (current: {current.get('theme', 'dark')})", value="theme"
                    ),
                    questionary.Choice("Calibrate", value="calibrate"),
                    questionary.Choice(
                        f"Catalog trust (current: {catalog_manifest})", value="catalog-trust"
                    ),
                    questionary.Choice("Catalog rollback", value="catalog-rollback"),
                    questionary.Choice(
                        f"Memory guard (current: {memory_guard_policy})", value="memory-guard"
                    ),
                    questionary.Choice(
                        f"Error reports (current: {error_reports_policy})", value="error-reports"
                    ),
                    questionary.Choice("← Back", value="back"),
                ],
            )
        )
        if choice is None or choice == "back":
            return

        if choice == "telemetry":
            endpoint = questionary.text(
                "Endpoint (blank to keep current, 'none' to clear):"
            ).ask()
            configure_telemetry(endpoint=endpoint or None)
        elif choice == "upload":
            action = _ask_select(
                questionary.select(
                    f"Uploads (current: {upload_policy}):",
                    choices=[
                        questionary.Choice("Always send", value="enable"),
                        questionary.Choice("Never send", value="disable"),
                        questionary.Choice("Ask every time", value="ask"),
                        questionary.Choice("← Back", value="back"),
                    ],
                )
            )
            if action is not None and action != "back":
                configure_upload(
                    enable=(action == "enable"),
                    disable=(action == "disable"),
                    ask=(action == "ask"),
                )
        elif choice == "version":
            action = _ask_select(
                questionary.select(
                    f"Update channel (current: {update_channel}):",
                    choices=[
                        questionary.Choice("Stable (main)", value="stable"),
                        questionary.Choice("Beta", value="beta"),
                        questionary.Choice("← Back", value="back"),
                    ],
                )
            )
            if action is not None and action != "back":
                configure_version(stable=(action == "stable"), beta=(action == "beta"))
        elif choice == "theme":
            # Previews, not a bare list of names - picking a color scheme
            # sight-unseen is exactly what this feature exists to avoid.
            action = _pick_theme_interactively(
                str(current.get("theme", "dark")), allow_back=True
            )
            if action is not None:
                configure_theme(set_name=action)
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
        elif choice == "catalog-rollback":
            if _ask_confirm("Roll back the recommendation catalog?"):
                catalog_rollback()
        elif choice == "memory-guard":
            action = _ask_select(
                questionary.select(
                    f"Memory guard policy (current: {memory_guard_policy}):",
                    choices=[
                        questionary.Choice("Ask before releasing OMM-owned memory", value="ask"),
                        questionary.Choice("Block instead of asking", value="block"),
                        questionary.Choice("Observe only (never block)", value="observe"),
                        questionary.Choice("← Back", value="back"),
                    ],
                )
            )
            if action is not None and action != "back":
                configure_memory_guard(policy=action, poll_seconds=None, low_memory_seconds=None)
        elif choice == "error-reports":
            action = _ask_select(
                questionary.select(
                    f"Error reports (current: {error_reports_policy}):",
                    choices=[
                        questionary.Choice("Ask once per `omm contribute` run", value="ask"),
                        questionary.Choice("Always send", value="enable"),
                        questionary.Choice("Never send (default)", value="disable"),
                        questionary.Choice("← Back", value="back"),
                    ],
                )
            )
            if action is not None and action != "back":
                configure_error_reports(
                    enable=(action == "enable"),
                    disable=(action == "disable"),
                    ask=(action == "ask"),
                )

        if not _ask_confirm("Change another setting?", default=True):
            return


@app.command()
@global_flags
def search(
    query: str,
    skip_unfit: bool = typer.Option(
        False,
        "--skip-unfit",
        help="If this hardware is predicted not to run a model, omit it "
        "from the results instead of listing it.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Show at most this many results."
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Only show results from this source: curated (omm's built-in/cached "
        "catalog, not a real host), huggingface, or modelscope.",
    ),
    skip_ms: bool = typer.Option(
        False,
        "--skip-ms",
        help="Don't query ModelScope. Its results need one extra network "
        "request per candidate repo, which can noticeably slow down search.",
    ),
) -> None:
    """Search curated models, cached candidates, and HuggingFace by name."""
    if provider is not None and provider not in ("curated", "huggingface", "modelscope"):
        err_console.print(
            f"[error]--provider must be one of: curated, huggingface, modelscope (got '{provider}').[/error]"
        )
        raise typer.Exit(2)
    if skip_ms and provider == "modelscope":
        err_console.print("[error]--skip-ms conflicts with --provider modelscope.[/error]")
        raise typer.Exit(2)
    json_output = _global_opts().json
    config = load_config()
    pool = (
        search_mod.local_candidate_pool(
            config.get("model_url"),
            manifest_url=config.get("catalog_manifest_url"),
            public_key=config.get("catalog_public_key"),
        )
        if provider in (None, "curated")
        else []
    )
    _handle_emergency_signal(predictor.load_cached_model())
    local_matches = search_mod.match_candidates(pool, query)

    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    from concurrent.futures import ThreadPoolExecutor

    query_huggingface = provider in (None, "huggingface")
    query_modelscope = provider in (None, "modelscope") and not skip_ms
    with ThreadPoolExecutor(max_workers=max(1, query_huggingface + query_modelscope)) as executor:
        hf_future = (
            executor.submit(search_mod.search_huggingface, query)
            if query_huggingface
            else None
        )
        ms_future = (
            executor.submit(search_mod.search_modelscope, query)
            if query_modelscope
            else None
        )
        hf_matches = [
            c
            for c in (hf_future.result() if hf_future else [])
            if c.get("repo_id") not in local_repo_ids
        ]
        ms_matches = [
            c
            for c in (ms_future.result() if ms_future else [])
            if c.get("repo_id") not in local_repo_ids
        ]

    combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches + ms_matches)
    if provider is not None:
        if provider == "curated":
            combined = [c for c in combined if not c.get("provider")]
        else:
            combined = [c for c in combined if c.get("provider") == provider]
    if not combined:
        err_console.print(f"[warning]No models found matching '{query}'.[/warning]")
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
            if limit is not None and len(refs) >= limit:
                break
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
                    console.print(f"[heading]==> {family}[/heading]")
                    header_printed = True
                if fits_hardware:
                    console.print(f"  [muted][{len(refs)}][/muted] [accent]{ref}[/accent]  [muted]{desc}[/muted]")
                else:
                    console.print(f"  [muted][{len(refs)}][/muted] {ref}  [error](predicted not to run on this hardware)[/error]")
        if not json_output and header_printed:
            console.print()
        if limit is not None and len(refs) >= limit:
            break

    session_cache.record_results(refs)
    if json_output:
        console.print_json(data=rows)
    elif refs:
        console.print(
            "[muted]Install with: omm install <number>  (e.g. omm install 1)[/muted]"
        )


def _print_install_suggestions(query: str) -> None:
    config = load_config()
    pool = search_mod.local_candidate_pool(
        config.get("model_url"),
        manifest_url=config.get("catalog_manifest_url"),
        public_key=config.get("catalog_public_key"),
    )
    _handle_emergency_signal(predictor.load_cached_model())
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

    err_console.print("[warning]Did you mean one of these?[/warning]")
    for s in suggestions:
        err_console.print(f"  - {search_mod.install_ref(s)}")


@app.command(name="link")
@global_flags
def link_models(
    directory: Path = typer.Argument(
        None,
        help="Optional model directory for an unsupported local AI app.",
    ),
    engine: str | None = typer.Option(
        None, "--engine", help="Only re-verify/repair links for this engine."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reclaim a destination omm doesn't recognize as its own "
        "(e.g. lost ownership record, or a file placed there by something "
        "else) by deleting it and relinking, instead of skipping it as a "
        "conflict.",
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
    zero-copy links when possible, with an explicit copy warning when Windows
    permissions and volume boundaries make that impossible."""
    _validate_engine(engine)
    if directory is not None and engine is not None:
        err_console.print("[error]--engine only applies without a directory argument.[/error]")
        raise typer.Exit(2)
    reg = registry.load_registry()
    if not reg:
        console.print("No models installed via omm yet.")
        raise typer.Exit(0)
    if engine is not None and not linker.is_engine_installed(engine):
        console.print(
            f"{_engine_label(engine)} isn't installed on this machine, "
            "so there's nothing to link into it."
        )
        raise typer.Exit(0)

    if directory is not None:
        directory = directory.expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            err_console.print(f"[error]Could not create {directory}: {error}[/error]")
            raise typer.Exit(1) from error
        linked_count = 0
        skipped_missing = 0
        copy_warnings: list[str] = []

        def report_copy(_source: Path, destination: Path, size_bytes: int) -> None:
            copy_warnings.append(
                f"{size_bytes / 1024**3:.1f} GiB copied to {destination}; "
                "a zero-copy Windows link was unavailable."
            )

        for filename, entry in reg.items():
            try:
                source = _managed_model_path(filename)
            except ModelResolutionError as error:
                err_console.print(f"[warning]{filename}: unsafe registry entry skipped: {error}[/warning]")
                skipped_missing += 1
                continue
            if not source.exists():
                skipped_missing += 1
                continue
            try:
                destination = linker.link_custom_directory(
                    source, directory, on_copy=report_copy, force=force
                )
            except linker.LinkError as error:
                err_console.print(f"[warning]{filename}: custom link skipped: {error}[/warning]")
                continue
            custom_links = list(entry.get("custom_links") or [])
            if str(destination) not in custom_links:
                custom_links.append(str(destination))
            registry.upsert_entry(filename, custom_links=custom_links)
            linked_count += 1
        for warning in copy_warnings:
            err_console.print(f"[warning]{warning}[/warning]")
        console.print(
            f"[success]{linked_count} model(s) linked into {directory}.[/success] "
            f"{skipped_missing} skipped (file missing)."
        )
        return

    relinked_count = 0
    skipped_missing = 0
    skipped_conflict = 0
    engines_to_process = (
        [s for s in linker.ENGINES if s.key == engine] if engine else linker.ENGINES
    )

    for filename, entry in reg.items():
        try:
            dest = _managed_model_path(filename)
        except ModelResolutionError as error:
            err_console.print(f"[warning]{filename}: unsafe registry entry skipped: {error}[/warning]")
            skipped_missing += 1
            continue
        if not dest.exists():
            skipped_missing += 1
            continue

        ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
        new_linked: dict[str, bool] = {}
        blocked = set(entry.get("link_blocked") or [])
        changed = False

        for spec in engines_to_process:
            if not linker.is_engine_installed(spec.key):
                continue
            try:
                warning = linker.link_engine(
                    spec.key,
                    dest,
                    repo_id=entry.get("repo_id"),
                    ollama_tag=ollama_tag,
                    force=force,
                )
                new_linked[spec.key] = True
                changed = True
                blocked.discard(spec.key)
                if warning:
                    err_console.print(f"[warning]{warning}[/warning]")
            except linker.LinkError as e:
                err_console.print(f"[warning]{filename}: {spec.label} link skipped: {e}[/warning]")
                blocked.add(spec.key)

        if blocked != set(entry.get("link_blocked") or []):
            registry.upsert_entry(filename, link_blocked=sorted(blocked))
        if changed:
            registry.upsert_entry(filename, linked=new_linked, ollama_name=ollama_tag)
            relinked_count += 1
        elif blocked:
            skipped_conflict += 1

    engine_suffix = f" (--engine {engine})" if engine is not None else ""
    console.print(
        f"[success]{relinked_count} model(s) relinked/verified{engine_suffix}.[/success] "
        f"{skipped_conflict} skipped (conflict). {skipped_missing} skipped (file missing)."
    )


@app.command(name="relink", hidden=True)
def relink() -> None:
    """Deprecated alias for `omm link`."""
    err_console.print("[warning]`omm relink` is deprecated; use `omm link`.[/warning]")
    link_models(directory=None, engine=None)


def _cleanup_incomplete_installs() -> int:
    if not MODELS_DIR.exists():
        return 0

    reg = registry.load_registry()
    removed = 0
    for path in MODELS_DIR.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(MODELS_DIR).as_posix()
        registry_filename: str | None = None
        companion_part: Path | None = None
        if relative.endswith(".gguf.part.ranges.json"):
            registry_filename = relative[: -len(".part.ranges.json")]
            companion_part = path.with_name(path.name.removesuffix(".ranges.json"))
        elif relative.endswith(".gguf.part.meta"):
            registry_filename = relative[: -len(".part.meta")]
            companion_part = path.with_name(path.name.removesuffix(".meta"))
        elif relative.endswith(".gguf.part"):
            registry_filename = relative[: -len(".part")]
        elif relative.endswith(".gguf"):
            registry_filename = relative
        if registry_filename is not None and (
            registry_filename not in reg
            or (companion_part is not None and not companion_part.exists())
        ):
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    for directory in sorted(
        (path for path in MODELS_DIR.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


@app.command()
@global_flags
def autoremove() -> None:
    """Clean up broken symlinks left in AI runner model directories.

    Removes symlinks left behind when a model's source .gguf was deleted
    without going through `omm uninstall`."""
    removed_by_engine: dict[str, int] = {}
    for spec in linker.ENGINES:
        if linker.is_engine_installed(spec.key):
            removed_by_engine[spec.label] = linker.autoremove_engine(spec.key)
    custom_removed = 0
    for entry in registry.load_registry().values():
        for destination in entry.get("custom_links", []):
            if isinstance(destination, str) and linker.autoremove_owned_link(Path(destination)):
                custom_removed += 1
    if custom_removed:
        removed_by_engine["custom"] = custom_removed

    if not any(removed_by_engine.values()):
        console.print("[success]No broken symlinks found.[/success]")
        return

    parts = [f"{count} broken {label} link(s)" for label, count in removed_by_engine.items() if count]
    console.print(f"[success]Removed {', '.join(parts) or '0 broken links'}.[/success]")


@app.command()
@global_flags
def cleanup() -> None:
    """Clean up leftover partial downloads and install cache files.

    Removes orphaned partial or unregistered .gguf downloads left behind
    in the models directory by interrupted or incomplete installs."""
    incomplete_removed = _cleanup_incomplete_installs()

    if incomplete_removed == 0:
        console.print("[success]No leftover install files found.[/success]")
        return

    console.print(f"[success]Cleaned up {incomplete_removed} incomplete install file(s).[/success]")


def _guard_benchmark_models(models: list[str]) -> None:
    # Ollama-only: matches registry entries by their Ollama tag, so LM
    # Studio modelKeys (never stored as `ollama_name`) simply find no entry
    # and are skipped below - `omm benchmark` against LM Studio has no
    # memory-guard pre-check yet (tracked as a known gap, see Task 3's
    # report; `omm contribute` does have one via `_guard_engine_load`).
    entries = registry.load_registry()
    ollama_entries = [
        (filename, value)
        for filename, value in entries.items()
        if isinstance(filename, str) and isinstance(value, dict)
    ]
    # Resolved once for every entry up front instead of once per (tag, entry)
    # pair below - each resolution can otherwise walk the whole Ollama
    # manifest tree (see issue #181), so `len(models) * len(entries)` calls
    # here used to mean that many full rescans.
    runtime_names = linker.resolve_ollama_runtime_names_batch(ollama_entries)
    for tag in models:
        entry = next(
            (
                value
                for filename, value in ollama_entries
                if memory_guard_mod._same_ollama_id(runtime_names[filename], tag)
            ),
            None,
        )
        size_bytes = entry.get("size_bytes") if isinstance(entry, dict) else None
        if not _positive_finite_number(size_bytes):
            continue
        required_gb = float(size_bytes) / (1024**3) * 1.2
        allowed, _runtime, _preloaded = _guard_ollama_load(tag, required_gb)
        if not allowed:
            raise typer.Exit(1)


def _lmstudio_installed_models() -> dict[str, dict]:
    """All installed LM Studio LLM models, keyed by modelKey, in the same
    reduced shape `linker.resolve_lmstudio_model()` returns - `omm
    benchmark`'s "all" expansion and free-form tag lookup for the LM
    Studio engine. Reuses linker's private LM Studio listing helper
    (`_lms_*` reuse is sanctioned by the plan's global constraints) rather
    than adding new subprocess/API logic; the field mapping below
    necessarily mirrors resolve_lmstudio_model's own (a known, pre-existing
    duplication in that function - see Task 1's review notes - not
    introduced by this change)."""
    lms_path = linker._lms_cli_path()
    if lms_path is None:
        return {}
    entries = linker._lmstudio_list_models(lms_path) or []
    models: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "llm":
            continue
        model_key = entry.get("modelKey")
        if not isinstance(model_key, str) or not model_key:
            continue
        quant = entry.get("quantization")
        if not isinstance(quant, dict):
            quant = {}
        models[model_key] = {
            "model_key": model_key,
            "architecture": entry.get("architecture"),
            "quantization_name": quant.get("name"),
            "quantization_bits": quant.get("bits"),
            "params_string": entry.get("paramsString"),
            "max_context_length": entry.get("maxContextLength"),
            "trained_for_tool_use": entry.get("trainedForToolUse", False),
        }
    return models


@app.command(name="benchmark")
@global_flags
def benchmark_cmd(
    models: list[str] = typer.Argument(
        ...,
        help="One or more already-installed model identifiers for the active "
        "engine (Ollama tags, or LM Studio modelKeys when Ollama isn't available).",
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
    # Numeric-ref resolution runs first, exactly as it always has - it only
    # ever transforms digit-only args (a numbered ref from the last
    # `omm search`/`omm list`) into an Ollama tag, and passes every other
    # arg through unchanged, so it's a safe no-op for LM Studio modelKeys
    # (never pure digits) and must not be deferred behind engine selection:
    # a bad numeric ref should fail immediately with its own clear error,
    # not after a daemon probe/prompt the argument was never going to need.
    models = [_resolve_benchmark_tag(m) for m in models]
    if "all" in models and models != ["all"]:
        err_console.print("[error]`all` must be the only argument.[/error]")
        raise typer.Exit(1)
    engine = _select_benchmark_engine()
    if engine is None:
        _print_no_engine_error("benchmark")
        raise typer.Exit(1)
    if not json_output:
        _print_engine_selection_notice(engine)
    engine, started_daemon = _ensure_engine_running(
        engine, "benchmark", assume_yes=_global_opts().yes
    )
    daemon_ref = {"proc": started_daemon}

    def stop_started_daemon() -> None:
        if daemon_ref["proc"] is not None:
            _stop_engine_daemon(engine, daemon_ref["proc"])
            daemon_ref["proc"] = None

    lmstudio_models: dict[str, dict] | None = None
    if engine == "lmstudio":
        try:
            installed = _lmstudio_installed_models()
        except BaseException:
            stop_started_daemon()
            raise
        if models == ["all"]:
            models = sorted(installed)
            if not models:
                err_console.print("[error]No models are installed in LM Studio to benchmark.[/error]")
                stop_started_daemon()
                raise typer.Exit(1)
            if not (_global_opts().quiet or json_output):
                console.print(f"[muted]Expanding 'all' to {len(models)} model(s): {', '.join(models)}[/muted]")
        unknown = [m for m in models if m not in installed]
        if unknown:
            err_console.print(
                "[error]Not installed in LM Studio: " + ", ".join(unknown)
                + ". Use the modelKey shown by `lms ls`.[/error]"
            )
            stop_started_daemon()
            raise typer.Exit(1)
        lmstudio_models = installed
    else:
        if models == ["all"]:
            try:
                models = quality_mod.list_benchmarkable_tags()
            except BaseException:
                stop_started_daemon()
                raise
            if not models:
                err_console.print("[error]No models are installed in Ollama to benchmark.[/error]")
                stop_started_daemon()
                raise typer.Exit(1)
            if not (_global_opts().quiet or json_output):
                console.print(f"[muted]Expanding 'all' to {len(models)} model(s): {', '.join(models)}[/muted]")
        try:
            _guard_benchmark_models(models)
        except BaseException:
            stop_started_daemon()
            raise
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = config_mod.EVALUATIONS_DIR / f"quality-{stamp}.json"
    # Advisory only: `omm benchmark` records evidence the caller reads and may
    # upload, so a busy machine is worth saying out loud before the run rather
    # than leaving it invisible in a number that looks tight.
    if not json_output:
        _background_cpu_load_is_high()
    try:
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[accent]{task.description}[/accent]"),
                TimeElapsedColumn(),
                console=console,
                disable=_global_opts().quiet or json_output,
            ) as progress:
                task_id = progress.add_task(
                    f"Benchmarking ({len(models)} model(s))...", total=len(models)
                )

                def _on_model_start(tag: str, index: int, total: int) -> None:
                    progress.update(
                        task_id,
                        description=f"Benchmarking {tag} ({index}/{total})",
                        completed=index - 1,
                    )

                def _on_daemon_event(message: str) -> None:
                    if not json_output:
                        progress.console.print(f"[warning]{message}[/warning]")

                report = quality_mod.collect_evidence(
                    models,
                    scan_hardware(),
                    pack_path=pack,
                    speed_runs=speed_runs,
                    engine=engine,
                    lmstudio_models=lmstudio_models,
                    confirm_performance_timeout=confirm_performance_timeout,
                    on_model_start=_on_model_start,
                    on_daemon_event=_on_daemon_event,
                    daemon_ref=daemon_ref,
                )
                progress.update(task_id, completed=len(models))
            quality_mod.write_evidence(report, output)
        except quality_mod.QualityEvaluationError as error:
            err_console.print(f"[error]{error}[/error]")
            raise typer.Exit(1) from error

        successes = [m for m in report["models"] if m.get("outcome", "success") == "success"]
        model_unfit = [m for m in report["models"] if m.get("outcome") == "model_unfit"]
        performance_unfit = [m for m in report["models"] if m.get("outcome") == "performance_unfit"]
        transient = [m for m in report["models"] if m.get("outcome") == "transient_error"]

        if successes and not json_output:
            table = _table(title="Localfit reproducible quality evidence")
            table.add_column("Model", style="accent")
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

        for entry in model_unfit if not json_output else ():
            err_console.print(
                f"[warning]{entry['tag']}: doesn't fit this hardware "
                f"({entry.get('failure_reason', 'unknown')})[/warning]"
            )
        for entry in performance_unfit if not json_output else ():
            err_console.print(
                f"[error]{entry['tag']}: confirmed twice that generation exceeds the "
                f"timeout on this hardware - performance_unfit "
                f"({entry.get('failure_reason', 'unknown')})[/error]"
            )
        for entry in transient if not json_output else ():
            reason = entry.get("failure_reason", "unknown")
            err_console.print(
                f"[warning]{entry['tag']}: temporary error, not a hardware verdict ({reason})[/warning]"
            )
            hint = _TRANSIENT_FAILURE_HINTS.get(reason)
            if hint:
                err_console.print(f"  [muted]{hint}[/muted]")

        if not json_output:
            console.print(f"[success]Saved reproducible local evidence to {output}.[/success]")
            console.print(
                "[muted]No generated text is stored. v8 telemetry includes a CPU/GPU "
                "generation score (never the model name), plus CPU architecture and "
                "core counts. aggregate numbers may be shared below. Not a "
                "leaderboard.[/muted]"
            )
        should_upload = (
            load_config().get("telemetry_send_policy") == "always"
            if json_output
            else _resolve_upload_decision(
                "Send these benchmark results to the server to help train the recommendation model?"
            )
        )
        if should_upload:
            registry_entries = registry.load_registry()
            for model in successes:
                entry = next(
                    (
                        e
                        for filename, e in registry_entries.items()
                        if isinstance(filename, str)
                        and isinstance(e, dict)
                        and memory_guard_mod._same_ollama_id(
                            linker.resolve_ollama_runtime_name(filename, e), model["tag"]
                        )
                    ),
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
                    engine=engine,
                )
            for entry in model_unfit + performance_unfit + transient:
                _report_failure_telemetry(entry, report.get("environment", {}))

        if json_output:
            console.print_json(data=report)
        else:
            console.print(
                f"[bold]Summary:[/bold] {len(successes)} succeeded, "
                f"{len(model_unfit)} model_unfit, {len(performance_unfit)} performance_unfit, "
                f"{len(transient)} transient_error",
                highlight=False,
            )
        if not successes:
            raise typer.Exit(1)
    finally:
        stop_started_daemon()


def _telemetry_send_failure_text() -> str:
    status = telemetry.last_send_status()
    if status is None:
        return "unknown send failure"
    if status.status_code is not None:
        detail = f": {escape(status.detail)}" if status.detail else ""
        return f"server rejected the event (HTTP {status.status_code}{detail})"
    if status.outcome == "send_failed_network":
        return "network request failed"
    if status.outcome == "skipped_no_endpoint":
        return "no valid telemetry endpoint is configured"
    return status.outcome.replace("_", " ")


def _telemetry_rejection_hint_text() -> str | None:
    """One muted follow-up for the send failure whose raw text is otherwise
    a dead end: `server rejected the event (HTTP 401: Permission denied)`.

    The hosted Firebase collector answers a *rules* rejection with 401 and
    that exact detail, while a missing or expired auth token reports the
    token problem in the same field - so the detail text, not the status
    code alone, is what identifies this case. Nothing about it is fixable
    locally, and the likeliest cause is deployed rules validating an older
    event shape than this omm version sends. Phrased as a possibility, not
    a diagnosis: the client is never told which rule fired."""
    status = telemetry.last_send_status()
    if status is None or status.status_code != 401:
        return None
    if "permission denied" not in status.detail.casefold():
        return None
    return (
        "[muted]Rules rejection, not a credential problem - nothing to fix on "
        "this machine. The collector's validation rules may be behind this omm "
        "version; worth reporting if it keeps happening.[/muted]"
    )


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
    failure_reason: str | None = None,
    engine: str = "ollama",
    speed_samples: list[float] | tuple[float, ...] | None = None,
    memory_measurement: dict | None = None,
    memory_estimate: contribute_memory.ContributionMemoryEstimate | None = None,
    host_cpu_load_percent: float | None = None,
) -> bool:
    if tokens_per_sec is None:
        # Not a real "it doesn't run" signal, so skip rather than polluting
        # the speed-regression training data. `failure_reason` (when known,
        # from a caught QualityEvaluationError) says what actually went
        # wrong - never assume it was the daemon unless that's confirmed,
        # since guessing wrong here is how the same "daemon" theory gets
        # re-fixed without ever being the real cause.
        if failure_reason is None:
            telemetry.log_attempt("skipped_daemon_unreachable", filename)
            console.print(
                f"[muted]Telemetry not sent - {_engine_label(engine)} daemon wasn't reachable "
                "during benchmark.[/muted]"
            )
        else:
            telemetry.log_attempt(f"skipped_{failure_reason}", filename)
            console.print(
                f"[muted]Telemetry not sent - benchmark failed ({failure_reason}).[/muted]"
            )
        return False
    info = scan_hardware()
    if size_bytes is None:
        try:
            model_file = _managed_model_path(filename)
        except ModelResolutionError:
            model_file = None
        size_bytes = (
            model_file.stat().st_size
            if model_file is not None and model_file.exists()
            else None
        )
    event = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "gpu_tflops": info.gpu_tflops,
        "model_installed": filename,
        "model_repo_id": repo_id,
        "model_provider": provider or "huggingface",
        "model_size_bytes": size_bytes,
        "engine": engine,
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
        "is_moe": metadata.get("is_moe") is True,
    }
    parameter_count = _number("parameter_count_b", "parameter_count_billions")
    if parameter_count is None:
        value = metadata.get("parameter_size")
        parameter_count = parse_param_count_billions(value) if isinstance(value, str) else None
    if parameter_count is None:
        parameter_count = candidate_parameter_count_billions(candidate)
    active_parameter_count = _number("active_parameter_count_b", "active_parameter_count_billions")
    if active_parameter_count is None:
        active_parameter_count = resolve_active_parameter_count_billions(
            candidate, parameter_count
        )
    elif parameter_count is not None:
        active_parameter_count = min(active_parameter_count, parameter_count)
    if candidate["is_moe"] and active_parameter_count is None:
        telemetry.log_attempt("skipped_moe_active_parameters_unknown", filename)
        console.print(
            "[muted]Telemetry not sent - this MoE model's active parameter count "
            "could not be verified.[/muted]"
        )
        return False
    quant_bits = _number("quant_bits")
    if quant_bits is None:
        value = metadata.get("quantization_level")
        quant_bits = parse_quant_bits(value) if isinstance(value, str) else None
    if quant_bits is None:
        quant_bits = candidate_quant_bits(candidate)
    digest = _normalize_model_digest(model_digest or metadata.get("digest"))
    safe_filename = _safe_model_filename(model_filename or filename)
    # LM Studio has no /api/ps equivalent and ignores the Ollama runtime
    # options omm computes, so omm can neither set nor observe the runtime
    # shape (runtime_profile, context_length, gpu_offload_percent,
    # cpu_threads, num_batch) of an LM Studio generation. Requiring that
    # block for every engine silently collapsed every LM Studio benchmark
    # to the v4 shape, throwing away the parameter/quant/CPU/engine-version
    # fields LM Studio *can* report honestly. v8 therefore carries LM Studio
    # rows with no runtime block at all rather than fabricated numbers, and
    # the Rules require those five keys to be absent when engine is
    # "lmstudio" so an unknown runtime can never be mistaken for a measured
    # one.
    runtime_is_observable = engine != "lmstudio"
    complete_runtime = _complete_runtime(runtime) if runtime_is_observable else None
    complete_cpu = _complete_cpu_metadata(info)
    complete_gpu = _complete_gpu_metadata(info)
    client_version = _client_version()
    if (
        parameter_count is not None and active_parameter_count is not None and quant_bits is not None
        and (complete_runtime is not None or not runtime_is_observable)
        and complete_cpu is not None
        and isinstance(engine_version, str) and engine_version
        and client_version is not None and sample_count >= 3
    ):
        # v8: same direct-metadata contract as v7, except cpu_score/cpu_tier
        # (locally computed, never the raw name) replace cpu_model, and
        # gpu_score/gpu_tier (same parser, run on the GPU name instead) are
        # attached whenever a GPU was detected at all. Do not send v6/v7
        # from new code: both stay read-only, backward-compatible schemas
        # for historical data already in Firebase.
        event.update(
            parameter_count_b=parameter_count,
            active_parameter_count_b=active_parameter_count,
            quant_bits=quant_bits,
            engine_version=engine_version,
            client_version=client_version,
            benchmark_version=8,
            outcome="success",
            **(complete_runtime or {}),
            **complete_cpu,
        )
        if complete_gpu:
            event.update(complete_gpu)
        if safe_filename is not None:
            event["model_filename"] = safe_filename
        if digest is not None:
            event["model_digest"] = digest
        # v9 is not "v8 plus memory data" - it is the contribute-v1
        # measurement profile, which asserts the run happened at exactly
        # context_length 1024 / num_batch 128 and that the memory estimate
        # was computed for that same configuration. omm applies that profile
        # through Ollama's options; LM Studio ignores them and reports
        # nothing back, so an LM Studio row could only claim conformance it
        # never had. Keep v9 Ollama-only and let LM Studio land at v8.
        if (
            memory_measurement is not None
            and memory_estimate is not None
            and runtime_is_observable
        ):
            before = memory_measurement.get("ram_available_before_gb")
            minimum = memory_measurement.get("ram_available_min_gb")
            after = memory_measurement.get("ram_available_after_gb")
            pressure = memory_measurement.get("memory_pressure_observed")
            memory_values = (before, minimum, after)
            if (
                all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and 0 <= value <= 1024
                    for value in memory_values
                )
                and isinstance(pressure, bool)
                and speed_samples is not None
                and len(speed_samples) >= 3
            ):
                mad_ratio = contribute_memory.speed_mad_ratio(speed_samples)
                # Only a reading that is genuinely a percentage is carried.
                # An unavailable sample stays absent rather than being sent
                # as a zero, which would claim an idle host omm never saw.
                # Rounded before it is classified, never after: 24.96 sent as
                # 25.0 would arrive labelled clean at a value the server
                # reads as loaded, and the whole row would be rejected.
                host_load = (
                    round(float(host_cpu_load_percent), 1)
                    if isinstance(host_cpu_load_percent, (int, float))
                    and not isinstance(host_cpu_load_percent, bool)
                    and math.isfinite(host_cpu_load_percent)
                    and 0 <= host_cpu_load_percent <= 100
                    else None
                )
                # Precedence: pressured > unstable > loaded > clean. The two
                # older signals are direct observations of this run's own
                # data - RAM dipped, or the samples disagreed - while host
                # load explains a run that otherwise looks fine. Ordering
                # them first means `loaded` refines what v9 would already
                # have called `clean` and never masks a defect the existing
                # signals caught. database.rules.json, localfit_server, and
                # the training importer encode this exact order.
                measurement_quality = (
                    "pressured"
                    if pressure
                    else "unstable"
                    if mad_ratio > 0.15
                    else "loaded"
                    if _cpu_load_is_high(host_load)
                    else "clean"
                )
                event.update(
                    benchmark_version=9,
                    measurement_profile="contribute-v1",
                    measurement_quality=measurement_quality,
                    ram_available_before_gb=round(float(before), 3),
                    ram_available_min_gb=round(float(minimum), 3),
                    ram_available_after_gb=round(float(after), 3),
                    memory_pressure_observed=pressure,
                    tokens_per_sec_mad_ratio=round(mad_ratio, 6),
                    memory_estimate_source=memory_estimate.source,
                    memory_estimate_confidence=memory_estimate.confidence,
                    estimated_mapped_weights_gb=round(
                        memory_estimate.mapped_weights_ram_gb, 3
                    ),
                    estimated_committed_ram_gb=round(
                        memory_estimate.committed_ram_gb, 3
                    ),
                    estimated_required_vram_gb=round(
                        memory_estimate.required_vram_gb, 3
                    ),
                )
                if host_load is not None:
                    event["host_cpu_load_percent"] = host_load
    telemetry.reset_send_status()
    sent = telemetry.send_event(event, force=True)
    if not sent:
        reason = _telemetry_send_failure_text()
        if load_config().get("telemetry_send_policy") == "always":
            console.print(
                f"[muted]Telemetry not sent: {reason}; queued for a later retry.[/muted]"
            )
        else:
            diagnostic = telemetry.last_failed_path()
            detail = (
                f"A diagnostic copy was saved locally to {diagnostic} and will not be "
                "retried without consent."
                if diagnostic.exists()
                else "This one-time upload was not queued."
            )
            console.print(f"[muted]Telemetry not sent: {reason}. {detail}[/muted]")
        hint = _telemetry_rejection_hint_text()
        if hint is not None:
            console.print(hint)
    elif not _global_opts().quiet:
        console.print("[muted]Benchmark result uploaded.[/muted]")
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
        "engine": environment.get("engine") or "ollama",
        "benchmark_version": 8,
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
    complete_gpu = _complete_gpu_metadata(info)
    if complete_gpu:
        event.update(complete_gpu)

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
        "is_moe": metadata.get("is_moe") is True,
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
    active_parameter_count = resolve_active_parameter_count_billions(
        candidate, parameter_count
    )
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

    telemetry.reset_send_status()
    sent = telemetry.send_event(event, force=True)
    if not sent:
        failure = _telemetry_send_failure_text()
        if load_config().get("telemetry_send_policy") == "always":
            console.print(
                f"[muted]Telemetry not sent for {tag}: {failure}; queued for a later retry.[/muted]"
            )
        else:
            diagnostic = telemetry.last_failed_path()
            detail = (
                f"A diagnostic copy was saved locally to {diagnostic} and will not be "
                "retried without consent."
                if diagnostic.exists()
                else "This one-time upload was not queued."
            )
            console.print(
                f"[muted]Telemetry not sent for {tag}: {failure}. {detail}[/muted]"
            )
        hint = _telemetry_rejection_hint_text()
        if hint is not None:
            console.print(hint)
    elif not _global_opts().quiet:
        console.print(f"[muted]Reported {tag} as {outcome}.[/muted]")
    return sent


def _report_contribute_failure_telemetry(outcome: "InstallOutcome") -> None:
    """Best-effort v7 failure event for a candidate `omm contribute` gave up
    on, so "this doesn't work on this class of hardware" gets recorded once
    instead of every future contributor's session silently rediscovering it
    from scratch. Only ever reports model_unfit or transient_error (per
    quality.outcome_for_failure_reason) - never performance_unfit, since
    that verdict requires the same-session confirm-twice flow `omm
    benchmark` uses, which `omm contribute` doesn't run."""
    if outcome.failure_reason is None:
        return
    model = {
        "tag": outcome.ollama_tag or outcome.filename,
        "outcome": quality_mod.outcome_for_failure_reason(outcome.failure_reason),
        "failure_reason": outcome.failure_reason,
        "model_metadata": outcome.model_metadata,
    }
    _report_failure_telemetry(model, environment={"engine": outcome.benchmark_engine or "ollama"})


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


def _complete_cpu_metadata(info: HardwareInfo) -> dict[str, str | int | float] | None:
    """Return direct-metadata (v8) CPU data only when it is useful for
    training. Never includes the raw CPU model name - only a locally
    computed ordinal score/tier from the same parser GPU names use (see
    docs/telemetry-v8.md)."""
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
    cpu_score, cpu_tier = parse_chip_score(model)
    return {
        "cpu_score": cpu_score,
        "cpu_tier": cpu_tier,
        "cpu_arch": arch,
        "cpu_physical_cores": physical,
        "cpu_logical_cores": logical,
    }


def _complete_gpu_metadata(info: HardwareInfo) -> dict[str, float] | None:
    """Return locally-computed v8 GPU chip score data, or None when no GPU
    was detected at all. Never includes the raw GPU name (see
    docs/telemetry-v8.md) - only the two numbers `parse_chip_score` derives
    from it."""
    name = getattr(info, "gpu_name", None)
    if not isinstance(name, str) or not name.strip():
        return None
    gpu_score, gpu_tier = parse_chip_score(name)
    return {"gpu_score": gpu_score, "gpu_tier": gpu_tier}


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
    skipped_low_disk: int = 0
    skipped_low_memory: int = 0
    deferred_low_memory: int = 0
    attempted_not_uploaded: int = 0
    daemon_restarts: int = 0
    given_up_on: int = 0
    machine_failures: int = 0
    exhausted: bool = False


_MAX_CONSECUTIVE_DAEMON_FAILURES = 3
_DAEMON_RESTART_BACKOFF_SECONDS = 5.0
# A candidate that produces no real benchmark result (tokens_per_sec is
# None) this many times in a row is treated as permanently broken for this
# session and given up on, instead of being re-offered by the queue forever
# - a single candidate that always fails (for whatever reason - a crash the
# daemon can't survive, a reproducible timeout, etc.) would otherwise
# consume the entire unattended run without ever producing an upload.
_MAX_CANDIDATE_BENCHMARK_FAILURES = 2
_MAX_CANDIDATE_MEMORY_DEFERRALS = 3
_DEFERRED_MEMORY_RECHECK_SECONDS = 30.0
_MIN_CONTRIBUTE_START_FREE_BYTES = 10 * 1024**3


def _residents_for_engine(engine: str) -> tuple[memory_guard_mod.ResidentModel, ...]:
    """Live resident-model list for whichever engine a contribute session
    picked. Must match the engine actually being benchmarked - querying
    Ollama's residents while running against LM Studio (or vice versa)
    would silently see an empty/wrong list and mis-plan the memory guard."""
    reg = registry.load_registry()
    if engine == "lmstudio":
        return memory_guard_mod.LMStudioManagedRuntime(reg).list_residents()
    return memory_guard_mod.OllamaManagedRuntime(reg).list_residents()


@dataclass
class _DeferredContribution:
    candidate: dict
    attempts: int = 0


def _cleanup_interrupted_install(filename: str) -> None:
    """Unload first, then unlink/delete; required for Windows file handles."""
    reg = registry.load_registry()
    found_name, entry = _lookup_entry(filename, reg)
    if entry:
        ollama_tag = linker.resolve_ollama_runtime_name(filename, entry)
        if benchmark.ollama_daemon_reachable():
            quality_mod.ensure_model_unloaded(ollama_tag)
        _remove_one(found_name, entry)
    else:
        _cleanup_incomplete_install(filename)


_BLOCK_REASON_TEXT = {
    "committed_ram_exceeds_commit_limit": (
        "required committed RAM exceeds the Windows commit limit"
    ),
    "committed_ram_exceeds_physical_capacity": (
        "required committed RAM exceeds physical capacity"
    ),
    "runtime_buffers_exceed_physical_capacity": (
        "required runtime buffers exceed physical capacity"
    ),
    "vram_exceeds_physical_capacity": "required VRAM exceeds this GPU",
}


def _memory_plan_reason_text(plan: contribute_memory.ContributionMemoryPlan) -> str:
    """Phrase a non-SAFE plan in terms of the budget that actually ran out."""
    if plan.decision is not contribute_memory.ContributionMemoryDecision.BLOCK:
        return "runtime buffer memory is temporarily unavailable"
    for reason in plan.reasons:
        text = _BLOCK_REASON_TEXT.get(reason)
        if text is not None:
            return text
    return "required memory exceeds this machine's capacity"


def _contribute_candidate_memory_plan(
    candidate: dict,
    *,
    hw: HardwareInfo | None = None,
    residents: tuple[memory_guard_mod.ResidentModel, ...] | None = None,
    engine: str = "ollama",
    memory_sample: contribute_memory.AvailableMemorySample | None = None,
    commit: WindowsCommitInfo | None = None,
    fetch_remote_metadata: bool = True,
) -> contribute_memory.ContributionMemoryPlan | None:
    """Plan a contribution load before any model bytes are downloaded.

    Published candidates normally carry ``size_bytes``; the tuning helper
    can also estimate size from a parameter count and quantization in the
    name.  Unknown-size candidates keep the old runtime guard as a fallback
    rather than being rejected on an estimate OMM cannot make.
    """
    current_hw = hw if hw is not None else scan_hardware()
    sized_candidate = dict(candidate)
    size_bytes = sized_candidate.get("size_bytes")
    if not _positive_finite_number(size_bytes):
        size_bytes = None

    provider = candidate.get("provider") or "huggingface"
    repo_id = candidate.get("repo_id")
    filename = candidate.get("filename")
    if (
        size_bytes is None
        and fetch_remote_metadata
        and isinstance(repo_id, str)
        and isinstance(filename, str)
    ):
        size_bytes = remote_file_size(provider, repo_id, filename)
    if size_bytes is None:
        estimated_size_gb = tuning.candidate_model_size_gb(candidate)
        if estimated_size_gb is not None:
            size_bytes = round(estimated_size_gb * 1024**3)
    if size_bytes is not None:
        sized_candidate["size_bytes"] = size_bytes

    # Resolve placement after adding the exact remote size (when available),
    # so offload planning and the memory estimate describe the same model.
    profile = tuning.recommend_contribute_settings(current_hw, sized_candidate)

    metadata = candidate.get("gguf_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    architecture = metadata.get("general.architecture")
    if fetch_remote_metadata and not isinstance(architecture, str):
        if isinstance(repo_id, str) and isinstance(filename, str):
            remote_base = remote_gguf_metadata(
                provider, repo_id, filename, {"general.architecture"}
            )
            if remote_base:
                metadata.update(remote_base)
                architecture = metadata.get("general.architecture")
            if isinstance(architecture, str) and architecture:
                remote_dimensions = remote_gguf_metadata(
                    provider,
                    repo_id,
                    filename,
                    contribute_memory.metadata_keys_for_architecture(architecture),
                )
                if remote_dimensions:
                    metadata.update(remote_dimensions)

    estimate = contribute_memory.estimate_candidate_memory(
        sized_candidate,
        current_hw,
        context_length=profile.context_length,
        num_batch=profile.num_batch,
        gpu_offload_percent=profile.gpu_offload_percent,
        metadata=metadata,
        mmap_weights=contribute_memory.weights_mmap_expected(current_hw),
    )
    if estimate is None:
        return None
    if memory_sample is None:
        memory_sample = contribute_memory.sample_available_memory(
            available_ram_gb,
            total_ram_gb=current_hw.ram_total_gb,
        )
    if residents is None:
        residents = _residents_for_engine(engine)
    managed = tuple(resident for resident in residents if resident.owned_by_omm)
    if current_hw.unified_memory:
        reclaimable_ram = sum(resident.size_gb for resident in managed)
        reclaimable_vram = 0.0
    else:
        reclaimable_ram = sum(
            resident.ram_gb if resident.ram_gb is not None else resident.size_gb
            for resident in managed
        )
        reclaimable_vram = sum(resident.vram_gb or 0.0 for resident in managed)
    return contribute_memory.plan_candidate_memory(
        estimate,
        current_hw,
        memory_sample,
        reclaimable_ram_gb=reclaimable_ram,
        reclaimable_vram_gb=reclaimable_vram,
        commit=(
            windows_commit_info()
            if commit is None and str(current_hw.os_name).strip().casefold() == "windows"
            else commit
        ),
    )


def _report_contribute_failure_cooldowns(history_refs: set[str]) -> set[str]:
    """Refs held back this session because their last attempts died on this
    machine's own memory rather than on anything about the model, printing
    one line each so the run never silently drops a candidate.

    A ref that has since produced a real benchmark is not held back - the
    success cleared its streak - so `history_refs` wins outright."""
    cooldowns = benchmark_history.failure_cooldowns()
    cooldown_refs = set(cooldowns) - set(history_refs)
    if not cooldown_refs or _global_opts().quiet:
        return cooldown_refs
    for cooldown_ref in sorted(cooldown_refs):
        record = cooldowns[cooldown_ref]
        expires = benchmark_history.cooldown_expires_at(record)
        until = (
            f"retrying after {expires.strftime('%Y-%m-%d %H:%M')} UTC"
            if expires is not None
            else "retrying once the cooldown lapses"
        )
        console.print(
            f"[muted]Skipping {record.get('filename') or cooldown_ref}: "
            f"{record.get('consecutive_machine_failures')} downloads in a row ended in "
            f"{record.get('reason')} on this machine - {until}.[/muted]"
        )
    return cooldown_refs


def _ensure_contribute_candidate_memory(
    artifact: dict,
    hw: HardwareInfo,
    history_refs: set[str],
    engine: str = "ollama",
) -> None:
    """Abort before the loop when every pending candidate is blocked now."""
    pending = [
        candidate
        for candidate in artifact.get("candidates", [])
        if not contribute_mod.matches_history(candidate, history_refs)
    ]
    if not pending:
        return

    # If even one pending candidate has no reliable estimate, retain the
    # existing post-link guard for that candidate instead of claiming the
    # whole session is impossible.  This also avoids touching the runtime
    # API merely to discover that preflight is inconclusive.
    if any(tuning.candidate_model_size_gb(candidate) is None for candidate in pending):
        return

    residents = _residents_for_engine(engine)
    memory_sample = contribute_memory.sample_available_memory(
        available_ram_gb,
        total_ram_gb=hw.ram_total_gb,
    )
    blocked_plans = []
    for candidate in pending:
        plan = _contribute_candidate_memory_plan(
            candidate,
            hw=hw,
            residents=residents,
            memory_sample=memory_sample,
            fetch_remote_metadata=False,
        )
        # An unknown estimate must fall through to the existing post-link
        # guard.  SAFE and WARN can proceed; WARN may release an OMM-owned
        # resident under the configured guard policy.
        if (
            plan is None
            or plan.decision is not contribute_memory.ContributionMemoryDecision.BLOCK
        ):
            return
        blocked_plans.append(plan)

    if not blocked_plans:
        return
    smallest = min(blocked_plans, key=lambda plan: plan.required_gb)
    err_console.print(
        "[error]omm contribute will not start because no unbenchmarked candidate "
        "has enough allocatable runtime memory. "
        f"The smallest committed-buffer estimate is {smallest.required_gb:.1f} GiB, "
        f"but only {smallest.available_gb:.1f} GiB remains after the "
        f"{smallest.reserve_gb:.1f} GiB emergency reserve. Close memory-heavy "
        "apps or reboot, then try again. No model was downloaded.[/error]"
    )
    raise typer.Exit(1)


def _ensure_contribute_start_space() -> None:
    """Refuse an unattended run when either model volume is already low."""
    volumes: dict[tuple[str, str | int], Path] = {}
    for path in (MODELS_DIR, linker.ollama_models_dir()):
        volumes.setdefault(linker.storage_volume_key(path), path)

    failures = []
    free_values = []
    for path in volumes.values():
        try:
            free = shutil.disk_usage(linker.disk_usage_path(path)).free
        except OSError as error:
            err_console.print(
                f"[error]Could not verify free disk space for {path}: {error}[/error]"
            )
            raise typer.Exit(1) from error
        free_values.append(free)
        if free < _MIN_CONTRIBUTE_START_FREE_BYTES:
            failures.append(f"{path}: {free / 1024**3:.1f} GiB free")

    if failures:
        err_console.print(
            "[error]omm contribute will not start with low disk space. Keep at least "
            f"{_MIN_CONTRIBUTE_START_FREE_BYTES / 1024**3:.0f} GiB free on every "
            "model volume before an unattended run. "
            + "; ".join(failures)
            + ".[/error]"
        )
        raise typer.Exit(1)
    if free_values:
        console.print(
            f"[muted]Disk preflight passed: {min(free_values) / 1024**3:.1f} GiB free "
            "on the tightest model volume. Each candidate is checked again before download.[/muted]"
        )


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
    """Background key-listener so Esc can interrupt `omm install` or
    `omm contribute` even mid-download/mid-benchmark, not just at a
    questionary prompt. No-ops (Ctrl+C is still the fallback) when stdin
    isn't a real terminal - tests, CI, and piped input all fall into this
    path. Uses `sys.stdin.isatty()` rather than session_cache.py's
    `os.ttyname()` idiom: that call doesn't exist on Windows at all, which
    used to skip starting this listener there entirely and left Esc
    permanently dead on Windows."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            if not sys.stdin.isatty():
                return
        except (AttributeError, ValueError, OSError):
            return
        target = self._run_windows if platform.system() == "Windows" else self._run_posix
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def _run_windows(self) -> None:
        """Poll Esc without consuming Ctrl+C or any other console input.

        `GetAsyncKeyState` reports *global* OS-wide key state, not input
        scoped to this console - alone, it would abort `omm contribute` if
        the user presses Escape while any other window has focus (e.g.
        alt-tabbed away during a long benchmark). Gated on
        `GetForegroundWindow() == GetConsoleWindow()` so Escape only counts
        while omm's own console is the one actually receiving keystrokes.
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            get_async_key_state = user32.GetAsyncKeyState
            get_async_key_state.argtypes = [ctypes.c_int]
            get_async_key_state.restype = ctypes.c_short
            get_foreground_window = user32.GetForegroundWindow
            get_console_window = ctypes.windll.kernel32.GetConsoleWindow
            escape_was_down = False
            while not self.stop_event.is_set():
                console_has_focus = get_foreground_window() == get_console_window()
                escape_is_down = console_has_focus and bool(get_async_key_state(0x1B) & 0x8000)
                if escape_is_down and not escape_was_down:
                    self.stop_event.set()
                    return
                escape_was_down = escape_is_down
                time.sleep(0.05)
        except Exception:
            pass  # best-effort; Ctrl+C still works as a fallback

    def _run_posix(self) -> None:
        try:
            from prompt_toolkit.input import create_input
            from prompt_toolkit.keys import Keys

            inp = create_input()
            with inp.raw_mode():
                if sys.platform == "win32":
                    # Win32Input.read_keys() already polls the console input
                    # buffer non-blockingly, so there's no fd to select() on
                    # (Windows select() only works on sockets anyway).
                    while not self.stop_event.is_set():
                        for key_press in inp.read_keys():
                            if key_press.key == Keys.Escape:
                                self.stop_event.set()
                        time.sleep(0.1)
                else:
                    import select

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
    queue,
    stop_event: threading.Event,
    refetch,
    quality_pack: dict | None = None,
    daemon_ref: dict | None = None,
    fetch_siblings=None,
    engine: str = "ollama",
) -> _ContributionStats:
    opts = _global_opts()
    stats = _ContributionStats(benchmarked=[])
    consecutive_daemon_failures = 0
    benchmark_failure_counts: dict[str, int] = {}
    deferred: dict[str, _DeferredContribution] = {}
    gpu_state: dict = {"force_cpu": False}
    engine_label = "LM Studio" if engine == "lmstudio" else "Ollama"
    while not stop_event.is_set():
        if not _engine_daemon_reachable(engine):
            err_console.print(
                f"[warning]{engine_label} daemon isn't reachable - it likely crashed mid-session. "
                "Attempting to restart it...[/warning]"
            )
            restarted = _start_engine_daemon(engine)
            if restarted is None:
                consecutive_daemon_failures += 1
                if consecutive_daemon_failures >= _MAX_CONSECUTIVE_DAEMON_FAILURES:
                    err_console.print(
                        f"[error]{engine_label} daemon won't come back after "
                        f"{consecutive_daemon_failures} attempts - stopping "
                        "omm contribute instead of looping unattended.[/error]"
                    )
                    break
                time.sleep(_DAEMON_RESTART_BACKOFF_SECONDS)
                continue
            if daemon_ref is not None:
                daemon_ref["proc"] = restarted
            stats.daemon_restarts += 1
            consecutive_daemon_failures = 0

        candidate = queue.next_candidate(refetch=refetch, fetch_siblings=fetch_siblings)
        if candidate is None:
            retryable = [
                (candidate_ref, item)
                for candidate_ref, item in deferred.items()
                if item.attempts < _MAX_CANDIDATE_MEMORY_DEFERRALS
            ]
            if retryable and hasattr(queue, "release_deferred"):
                if not opts.quiet:
                    console.print(
                        f"[muted]Waiting {int(_DEFERRED_MEMORY_RECHECK_SECONDS)}s for "
                        f"memory before reconsidering {len(retryable)} deferred "
                        "candidate(s)...[/muted]"
                    )
                if stop_event.wait(_DEFERRED_MEMORY_RECHECK_SECONDS):
                    break
                for candidate_ref, _item in retryable:
                    queue.release_deferred(candidate_ref)
                continue
            if not opts.quiet:
                if deferred:
                    console.print(
                        "[muted]No deferred candidate became memory-safe during this "
                        "session.[/muted]"
                    )
                else:
                    console.print("[muted]No more candidates available for this hardware.[/muted]")
            stats.exhausted = not deferred
            break

        display_name = candidate.get("name", candidate["filename"])
        ref_str = contribute_mod.ref(candidate)
        if not opts.quiet:
            console.print(f"[accent]Trying {display_name}...[/accent]")

        memory_plan = _contribute_candidate_memory_plan(candidate, engine=engine)
        if memory_plan is not None and not opts.quiet:
            memory_message = (
                "[muted]Memory preflight before download: "
                f"committed RAM {memory_plan.estimate.committed_ram_gb:.2f} GiB; "
                f"runtime buffers {memory_plan.runtime_buffer_required_gb:.2f} GiB; "
                f"mmap-backed weights {memory_plan.estimate.mapped_weights_ram_gb:.2f} GiB; "
                f"median available {memory_plan.sample.median_gb:.2f} GiB; "
            )
            if memory_plan.commit_available_gb is not None:
                memory_message += (
                    f"commit headroom {memory_plan.commit_available_gb:.2f} GiB; "
                )
            memory_message += (
                f"emergency reserve {memory_plan.sample.reserve_gb:.2f} GiB; "
                f"estimate source {memory_plan.estimate.source}.[/muted]"
            )
            console.print(memory_message)
        if (
            memory_plan is not None
            and memory_plan.decision
            is not contribute_memory.ContributionMemoryDecision.SAFE
        ):
            item = deferred.setdefault(ref_str, _DeferredContribution(candidate))
            item.attempts += 1
            if item.attempts == 1:
                stats.deferred_low_memory += 1
            reason = _memory_plan_reason_text(memory_plan)
            err_console.print(
                f"[warning]Deferring {candidate['filename']} before download: {reason}. "
                f"Committed RAM estimate {memory_plan.estimate.committed_ram_gb:.1f} GiB, "
                f"runtime buffers {memory_plan.runtime_buffer_required_gb:.1f} GiB, "
                f"mapped weights {memory_plan.estimate.mapped_weights_ram_gb:.1f} GiB, "
                f"median available {memory_plan.sample.median_gb:.1f} GiB.[/warning]"
            )
            if hasattr(queue, "defer"):
                queue.defer(ref_str)
                if item.attempts >= _MAX_CANDIDATE_MEMORY_DEFERRALS:
                    stats.skipped_low_memory += 1
            else:
                # Compatibility for third-party/fake queues implementing the
                # older protocol. The real queue always supports deferral.
                queue.mark_seen(ref_str)
                stats.skipped_low_memory += 1
            continue

        # The check above (using this same engine-aware memory_plan) already
        # deferred or skipped anything not SAFE, so reaching here just means
        # this candidate is clear to proceed - drop any stale deferred-retry
        # bookkeeping for it.
        deferred.pop(ref_str, None)

        try:
            provider = validate_provider(candidate.get("provider") or "huggingface")
            repo_id = validate_repo_id(candidate["repo_id"])
            filename = validate_model_filename(candidate["filename"])
            resolved = ResolvedModel(
                url=download_url(provider, repo_id, filename),
                filename=filename,
                repo_id=repo_id,
                provider=provider,
            )
            outcome = _install_impl(
                resolved,
                auto_upload=True,
                skip_unfit=True,
                stop_event=stop_event,
                use_quality_eval=True,
                quality_pack=quality_pack,
                link_only_engine=engine,
                enforce_memory_guard=True,
                gpu_state=gpu_state,
                benchmark_engine=engine,
                contribute_mode=True,
                contribution_memory_estimate=(
                    memory_plan.estimate if memory_plan is not None else None
                ),
            )
        except InstallInterrupted as e:
            _cleanup_interrupted_install(e.filename)
            break
        except KeyboardInterrupt:
            # On Windows Ctrl+C is a console control event, not the Esc
            # listener's stop_event. It can interrupt download, checksum,
            # linking, or the isolated evaluator directly. Convert it to the
            # same unload-before-delete cleanup path while the active filename
            # is still known instead of letting it escape and strand a GGUF.
            stop_event.set()
            _cleanup_interrupted_install(filename)
            break
        except (DownloadError, ModelResolutionError, linker.LinkError) as e:
            err_console.print(f"[warning]Skipping {candidate['filename']}: {e}[/warning]")
            continue

        if outcome.tokens_per_sec is None and not _engine_daemon_reachable(engine):
            # Daemon died *during* this candidate's own download/benchmark
            # (as opposed to between candidates, which the check at the top
            # of the loop already catches). The model is already downloaded
            # and linked, so retry it once after restarting the daemon
            # instead of throwing away the download and re-fetching it as a
            # "new" candidate on the next iteration.
            err_console.print(
                f"[warning]{engine_label} daemon crashed while benchmarking {display_name} - "
                "restarting it and retrying this model once...[/warning]"
            )
            restarted = _start_engine_daemon(engine)
            if restarted is None:
                err_console.print(
                    f"[error]Couldn't restart the {engine_label} daemon - giving up on "
                    f"{display_name} for now.[/error]"
                )
                start_error = benchmark.last_daemon_start_error()
                error_report.queue_report(
                    trigger="daemon_restart_giveup",
                    error_type="DaemonRestartFailed",
                    message=(
                        f"{engine_label} daemon crashed while benchmarking and could not be "
                        f"restarted: {start_error or 'unknown startup failure'}"
                    ),
                    catalog_ref=error_report.catalog_ref(
                        candidate.get("repo_id"), candidate.get("filename")
                    ),
                    engine=engine,
                )
            else:
                if daemon_ref is not None:
                    daemon_ref["proc"] = restarted
                stats.daemon_restarts += 1
                try:
                    outcome = _install_impl(
                        resolved,
                        auto_upload=True,
                        skip_unfit=True,
                        stop_event=stop_event,
                        use_quality_eval=True,
                        quality_pack=quality_pack,
                        link_only_engine=engine,
                        enforce_memory_guard=True,
                        gpu_state=gpu_state,
                        benchmark_engine=engine,
                        contribute_mode=True,
                        contribution_memory_estimate=(
                            memory_plan.estimate if memory_plan is not None else None
                        ),
                    )
                except InstallInterrupted as e:
                    _cleanup_interrupted_install(e.filename)
                    break
                except KeyboardInterrupt:
                    stop_event.set()
                    _cleanup_interrupted_install(filename)
                    break
                except (DownloadError, linker.LinkError) as e:
                    err_console.print(f"[warning]Skipping {candidate['filename']}: {e}[/warning]")
                    continue

        if outcome.failure_reason in {
            "memory_allocation_blocked",
            "memory_allocation_deferred",
        }:
            reg = registry.load_registry()
            found_name, entry = _lookup_entry(outcome.filename, reg)
            if entry:
                _remove_one(found_name, entry)
            item = deferred.setdefault(ref_str, _DeferredContribution(candidate))
            item.attempts += 1
            if item.attempts == 1:
                stats.deferred_low_memory += 1
            out_of_retries = item.attempts >= _MAX_CANDIDATE_MEMORY_DEFERRALS
            if hasattr(queue, "defer"):
                queue.defer(ref_str)
                if out_of_retries:
                    stats.skipped_low_memory += 1
            else:
                queue.mark_seen(ref_str)
                stats.skipped_low_memory += 1
                out_of_retries = True
            if out_of_retries:
                # This candidate has now burned its whole in-run retry budget
                # (and one download per attempt) against live memory. Record
                # it exactly once per session, so the streak that drives the
                # cross-run cooldown counts sessions rather than retries.
                benchmark_history.record_benchmark_failure(
                    ref_str,
                    repo_id=outcome.repo_id,
                    filename=outcome.filename,
                    reason=outcome.failure_reason,
                    engine=outcome.benchmark_engine or engine,
                )
            err_console.print(
                f"[warning]Memory changed before {candidate['filename']} could load; "
                "cleaned it up and deferred a bounded retry.[/warning]"
            )
            continue

        if outcome.skipped_unfit:
            stats.skipped_unfit += 1
            # Hardware fit doesn't change mid-session, so a candidate the
            # predictor already rejected once needs no second look this
            # run. Without this, once every genuinely-viable candidate is
            # exhausted (queue.py's Phase B "below" side), the "above"
            # side of unfit candidates never becomes fully seen either -
            # next_candidate() always has one more unfit entry to hand
            # back, so the loop spins at machine speed forever instead of
            # reaching "No more candidates" or trying refetch.
            queue.mark_seen(ref_str)
            continue

        if outcome.skipped_low_disk:
            stats.skipped_low_disk += 1
            # Free space doesn't reliably change mid-session either, and a
            # later `omm contribute` run will re-check it from scratch -
            # same rationale as the skipped_unfit case just above.
            queue.mark_seen(ref_str)
            continue

        reg = registry.load_registry()
        fn, entry = _lookup_entry(outcome.filename, reg)
        if entry:
            _remove_one(fn, entry)

        # A completed attempt has just unloaded and deleted its model. Memory
        # conditions may therefore have improved; let bounded deferred items
        # re-enter ranking instead of waiting for a new contribute session.
        if hasattr(queue, "release_deferred"):
            for deferred_ref, deferred_item in deferred.items():
                if deferred_item.attempts < _MAX_CANDIDATE_MEMORY_DEFERRALS:
                    queue.release_deferred(deferred_ref)

        if outcome.tokens_per_sec is not None and outcome.telemetry_sent:
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
            if outcome.tokens_per_sec is None:
                # Leave a trace on disk. record_benchmarked() is success-only
                # by design, so without this a failed attempt is invisible to
                # the next session, which re-selects the same candidate and
                # re-downloads every byte of it.
                benchmark_history.record_benchmark_failure(
                    ref_str,
                    repo_id=outcome.repo_id,
                    filename=outcome.filename,
                    reason=outcome.failure_reason,
                    engine=outcome.benchmark_engine or engine,
                )
                if benchmark_history.is_machine_related_failure(outcome.failure_reason):
                    # The model is downloaded, benchmarked-and-cancelled, and
                    # already deleted again. Retrying it inside this same run
                    # means re-downloading it in full to meet the same
                    # machine condition that just cancelled it - free memory
                    # does not recover on the timescale of one loop
                    # iteration. Hand the run to the other candidates.
                    err_console.print(
                        f"[warning]{display_name} failed after downloading "
                        f"({outcome.failure_reason}) - not retrying it this session, to "
                        "avoid downloading it again for the same result.[/warning]"
                    )
                    queue.mark_seen(ref_str)
                    stats.machine_failures += 1
                    _report_contribute_failure_telemetry(outcome)
                    continue
                # No real benchmark result came back at all (as opposed to a
                # real result whose upload just failed/was declined - that
                # case is worth retrying, this one keeps producing nothing).
                # Give up on this exact candidate after it fails enough
                # times in a row, instead of letting the queue re-offer the
                # one broken model forever while every other candidate goes
                # untried.
                count = benchmark_failure_counts.get(ref_str, 0) + 1
                benchmark_failure_counts[ref_str] = count
                if count >= _MAX_CANDIDATE_BENCHMARK_FAILURES:
                    err_console.print(
                        f"[warning]{display_name} has failed to produce a benchmark result "
                        f"{count}x this session - giving up on it and moving on.[/warning]"
                    )
                    queue.mark_seen(ref_str)
                    stats.given_up_on += 1
                    _report_contribute_failure_telemetry(outcome)

    return stats


def _print_contribution_summary(
    stats: _ContributionStats,
    duration_seconds: float,
    before_count: int | None,
    after_count: int | None,
    *,
    total_candidates: int | None = None,
    covered_candidates: int | None = None,
    succeeded_candidates: int | None = None,
) -> None:
    minutes, seconds = divmod(int(duration_seconds), 60)
    console.print("=" * 70)
    console.print("[bold]omm contribute: session summary[/bold]")
    console.print(f"Duration: {minutes}m {seconds}s")
    console.print(f"Models benchmarked+uploaded: {len(stats.benchmarked)}")
    for name, tokens_per_sec in stats.benchmarked:
        console.print(f"  - {name:<40} {tokens_per_sec:.1f} tok/s")
    console.print(f"Skipped (predicted not to fit this hardware): {stats.skipped_unfit}")
    console.print(f"Skipped (not enough disk space): {stats.skipped_low_disk}")
    console.print(f"Deferred before download (live memory pressure): {stats.deferred_low_memory}")
    console.print(f"Still blocked after bounded memory retries: {stats.skipped_low_memory}")
    console.print(f"Attempted but not uploaded (kept for retry): {stats.attempted_not_uploaded}")
    if stats.machine_failures:
        console.print(
            f"[warning]Failed after downloading on live machine conditions: "
            f"{stats.machine_failures} candidate(s), not retried this session.[/warning]"
        )
    if stats.given_up_on:
        console.print(
            f"[warning]Gave up on {stats.given_up_on} candidate(s) after repeated "
            "benchmark failures this session.[/warning]"
        )
    if stats.daemon_restarts:
        console.print(
            f"[warning]Ollama daemon was found dead and restarted {stats.daemon_restarts}x "
            "during this session.[/warning]"
        )
    if before_count is not None and after_count is not None:
        console.print(
            f"Global telemetry dataset: {before_count} -> {after_count} rows "
            f"({after_count - before_count:+d})"
        )
        console.print(
            "  [muted](delta may include uploads from other contributors during this session)[/muted]"
        )
    console.print("=" * 70)
    if stats.exhausted and total_candidates is not None and covered_candidates is not None:
        console.print(
            "[success]Ω Thank you for contributing![/success] Every model "
            "currently published for this hardware has now been benchmarked or "
            f"evaluated ({covered_candidates}/{total_candidates} candidates covered"
            + (
                f", {succeeded_candidates} of them successfully"
                if succeeded_candidates is not None
                else ""
            )
            + "). There is nothing left for `omm contribute` to try on this "
            "machine right now - it will pick up again automatically once new "
            "candidates are published or the recommendation model is retrained."
        )


@app.command()
@global_flags
def contribute(
    report_errors: bool = typer.Option(
        False,
        "--report-errors",
        help=(
            "Send scrubbed error reports from this run only (does not change the saved "
            "policy; ignored if error reports are explicitly turned off)."
        ),
    ),
) -> None:
    """Benchmark models in a loop to improve `omm recommend`.

    Repeatedly installs, benchmarks, and uploads telemetry for
    hardware-fit models until Esc is pressed, growing the training dataset
    behind `omm recommend`. Deletes each model after benchmarking it (even
    successful ones) to keep disk usage bounded."""
    yes = _global_opts().yes
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "never":
        err_console.print(
            "[error]omm contribute requires benchmark uploads to be enabled. "
            "Run `omm setting upload --enable` or `--ask` first.[/error]"
        )
        raise typer.Exit(1)
    # Resolve the error-report consent here, once, for the same reason the
    # upload policy is resolved here: the loop below runs unattended and
    # must never stop to ask. A "no" only skips the reports - unlike the
    # upload policy it never blocks the run, since error reports are an
    # extra, not what `omm contribute` exists to do.
    error_report.set_run_consent(_resolve_error_report_decision(report_errors))
    if error_report.run_consent():
        flushed = error_report.flush_pending(max_retries=10, force=True)
        if flushed:
            console.print(
                f"[muted]Sent {flushed} error report(s) queued by an earlier run.[/muted]"
            )
    _ensure_contribute_start_space()
    # Engine availability is a preflight, not part of the expensive-work
    # consent. Do it first so users are never asked to approve bandwidth,
    # disk, and compute for a run this machine cannot start.
    engine = _select_benchmark_engine()
    if engine is None:
        _print_no_engine_error("contribute")
        raise typer.Exit(1)
    _print_engine_selection_notice(engine)
    # The consent/warning banner below (inside the try block) is deferred
    # until after the checks that can prove the session impossible - no
    # point asking for approval on a run that can't start anyway. The
    # `finally` block further down always stops whatever daemon we start
    # here, on every exit path, so no manual stop is needed on early exits.
    engine, started_daemon = _ensure_engine_running(engine, "contribute", assume_yes=yes)
    daemon_ref = {"proc": started_daemon}
    try:
        # Everything that can prove the session impossible belongs before the
        # expensive-work consent. These checks do not download model tensors.
        try:
            quality_pack, _ = quality_mod.load_pack()
        except quality_mod.QualityEvaluationError as error:
            err_console.print(f"[error]Could not load the quality pack: {error}[/error]")
            raise typer.Exit(1) from error

        config = load_config()
        artifact, _ = _load_recommendation_with_change_note(config)
        if not artifact or not artifact.get("candidates"):
            err_console.print(
                "[error]No trained recommendation model available - can't select candidates.[/error]"
            )
            raise typer.Exit(1)

        total_candidates = len(artifact["candidates"])
        prior_state = contribute_state.load()
        if prior_state is not None and prior_state.get("total_candidates") == total_candidates:
            err_console.print(
                "[warning]Heads up: a previous omm contribute session already "
                "covered every candidate currently published for this hardware "
                f"({prior_state.get('covered_candidates')}/{total_candidates}, as of "
                f"{prior_state.get('exhausted_at', 'an earlier run')}). You likely have "
                "nothing new to benchmark unless the catalog has grown since then - "
                "this run will confirm that quickly rather than find anything new.[/warning]"
            )

        hw = scan_hardware()
        history_refs = benchmark_history.loaded_refs()
        cooldown_refs = _report_contribute_failure_cooldowns(history_refs)
        queue = contribute_mod.ContributionQueue(
            artifact, hw, history_refs, excluded_refs=cooldown_refs
        )
        _ensure_contribute_candidate_memory(
            artifact, hw, history_refs | cooldown_refs, engine=engine
        )

        if policy == "always" and not config.get("contribute_always_ack"):
            err_console.print(
                "[warning]Upload policy is 'always' - every benchmark result from this "
                "and future omm contribute runs will be sent to the server without "
                "asking each time.[/warning]"
            )
            if not yes and not _ask_confirm("Continue?"):
                err_console.print("[warning]Cancelled.[/warning]")
                raise typer.Exit(0)
            config_mod.update_config(contribute_always_ack=True)

        err_console.print("[warning]omm contribute - before you start:[/warning]")
        contribute_notice_lines = [
            "Downloads, benchmarks, and deletes GGUF models repeatedly until you press Esc",
            "Uses real bandwidth, disk space, and compute; runs unattended "
            "(no per-model confirmation)",
            f"Uploads every benchmark result per your current upload policy ({policy})",
            "Reserves space per candidate (central GGUF + worst-case engine copy + headroom); "
            "skips anything that won't fit",
        ]
        if engine == "ollama":
            # The precise, GGUF-based memory estimator (commit-limit gating,
            # measurement-stability defer/retry) only understands Ollama
            # tags today - see _contribute_candidate_memory_plan. LM Studio
            # sessions still get a memory check, just the coarser
            # engine-generic one, so these two claims would be misleading.
            contribute_notice_lines += [
                "Uses a fixed 1024-token context and 128-token batch for comparable results",
                "Gates committed runtime memory before download; monitors paging and "
                "measurement stability while running",
                "Defers transient memory shortages up to three times instead of losing "
                "the candidate",
            ]
        contribute_notice_lines.append(
            "Each benchmark has a 10-minute cutoff, with a status line every 30s"
        )
        for line in contribute_notice_lines:
            err_console.print(f"  [warning]- {line}[/warning]")
        if platform.system() == "Windows":
            err_console.print(
                "  [warning]- Windows: antivirus scanning may delay first model loads - "
                "don't disable Defender, but avoid other heavy disk activity for "
                "comparable results[/warning]"
            )
        if not yes and not _ask_confirm("Start contributing compute now?"):
            err_console.print("[warning]Cancelled.[/warning]")
            raise typer.Exit(0)

        endpoint = config.get("telemetry_endpoint")
        before_count = _telemetry_row_count(endpoint) if endpoint else None

        def refetch():
            return _load_recommendation_with_change_note(config)

        listener = _EscListener()
        listener.start()
        start_time = time.monotonic()
        try:
            stats = _run_contribution_loop(
                queue,
                listener.stop_event,
                refetch,
                quality_pack=quality_pack,
                daemon_ref=daemon_ref,
                fetch_siblings=_fetch_sibling_candidates,
                engine=engine,
            )
        finally:
            listener.stop_event.set()

        cleanup()
        autoremove()

        after_count = _telemetry_row_count(endpoint) if endpoint else None
        duration = time.monotonic() - start_time
        covered_candidates = len(queue.history_refs)
        _print_contribution_summary(
            stats,
            duration,
            before_count,
            after_count,
            total_candidates=total_candidates,
            covered_candidates=covered_candidates,
            succeeded_candidates=len(benchmark_history.loaded_refs()),
        )
        if stats.exhausted:
            contribute_state.record_exhausted(total_candidates, covered_candidates)
    finally:
        if daemon_ref["proc"] is not None:
            _stop_engine_daemon(engine, daemon_ref["proc"])
        # Everything this session queued goes out here, after the loop, so
        # no benchmark ever waited on an HTTP round trip for a report.
        if error_report.run_consent():
            sent = error_report.flush_pending(max_retries=50, force=True)
            if sent and not _global_opts().quiet:
                console.print(f"[muted]Sent {sent} error report(s) from this session.[/muted]")
        error_report.set_run_consent(None)


def _known_command_names() -> set[str]:
    """Every subcommand name omm registers, including command groups."""
    names: set[str] = set()
    for command in getattr(app, "registered_commands", []):
        name = command.name or getattr(command.callback, "__name__", "")
        if name:
            names.add(name.replace("_", "-"))
    for group in getattr(app, "registered_groups", []):
        name = group.name or getattr(getattr(group.typer_instance, "info", None), "name", None)
        if name:
            names.add(name)
    return names


# Resolved once, at import, after every command has registered itself: this
# must stay correct even when something has replaced the module-level `app`
# (the crash hook's own tests do exactly that).
_REGISTERED_COMMAND_NAMES = frozenset(_known_command_names())


def _crash_subcommand(argv: list[str] | None = None) -> str | None:
    """Which subcommand a crash happened under, or None when it can't be
    told.

    Deliberately matched against the registered names instead of echoing
    the first argument back: a command line can carry search queries,
    URLs, and local file paths, and none of that belongs in a report.
    """
    tokens = sys.argv[1:] if argv is None else argv
    known = _REGISTERED_COMMAND_NAMES
    for token in tokens:
        if token.startswith("-"):
            continue
        return token if token in known else None
    return None


def _queue_crash_report(error: BaseException) -> None:
    """Trigger 3: record an unhandled crash for later sending.

    Queue only. Prompting for consent right after a crash would be a poor
    trade for the user, and a synchronous upload would delay the traceback
    they actually need, so the send happens on a later run (immediately for
    an `always` policy, at the next `omm contribute` for `ask`).
    """
    try:
        error_report.queue_report(error, trigger="crash", subcommand=_crash_subcommand())
    except Exception:
        pass


def main() -> None:
    """Console-script entry point (see pyproject.toml [project.scripts]).
    Catches disk-full errors that escape every local handler - e.g. a JSON
    write during `omm autoremove` - and prints one clean line instead of
    Typer's default traceback. Everything else is queued as an opt-in error
    report and then propagates untouched, so a genuine bug still surfaces as
    a normal traceback and can be reported."""
    try:
        app()
    except InsufficientDiskSpaceError as e:
        err_console.print(f"[error]{e}[/error]")
        raise SystemExit(1) from None
    except PermissionError as e:
        target = f" ({e.filename})" if e.filename else ""
        errors.print_cli_error(
            err_console,
            f"Permission denied{target}: {e.strerror or e}.",
            fix="Check that the file or directory is writable by your user, "
            "and that no other program (or a differently-owned daemon) has it open.",
        )
        raise SystemExit(1) from None
    except OSError as e:
        if e.errno == errno.ENOSPC:
            err_console.print(
                "[error]Not enough disk space to complete this operation. "
                "Free up space and try again.[/error]"
            )
            raise SystemExit(1) from None
        _queue_crash_report(e)
        raise
    except Exception as e:
        # SystemExit and KeyboardInterrupt are BaseException, so a normal
        # `typer.Exit` (including every `raise typer.Exit(1)` error path)
        # and a Ctrl+C never reach here - only real crashes do.
        _queue_crash_report(e)
        raise


if __name__ == "__main__":
    main()
