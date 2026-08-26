"""Start a local AI runner with an omm-installed model, for `omm run`.

omm's install flow ends with the GGUF linked into every detected runner,
but until now the user still had to leave the terminal, find the right
app, and locate the model inside it by hand. This module closes that gap:
one `launch(engine, ...)` call per engine that either hands the terminal
to an interactive chat (Ollama), starts the runner with the model
preloaded (KoboldCpp, text-generation-webui), or at least opens the GUI
app so the model is one click away (LM Studio, Jan, AnythingLLM, Msty).

Every launcher is best-effort and returns a `LaunchResult` rather than
raising: `omm run` prints `result.message` and exits non-zero when
`result.ok` is False. Nothing here touches the registry or links.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from omm import linker

# Preference order when the user did not say which runner to use: the
# ones that can actually start a chat with the model preloaded come
# first; the GUI-only launches (where omm can open the app but not pick
# the model inside it) come last.
ENGINE_PRIORITY: tuple[str, ...] = (
    "ollama",
    "koboldcpp",
    "lmstudio",
    "jan",
    "textgenwebui",
    "anythingllm",
    "mstystudio",
)

KOBOLDCPP_DEFAULT_URL = "http://localhost:5001"


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    message: str
    # True when `launch` blocked on an interactive session that has now
    # ended (Ollama chat), False when it started something in the
    # background and returned immediately.
    interactive: bool = False


# --- Shared helpers ----------------------------------------------------------


def _windows_programs_roots() -> list[Path]:
    # Apps omm installed itself live under OMM_HOME/apps; vendor defaults
    # are %LOCALAPPDATA%\Programs and Program Files.
    roots: list[Path] = [linker.engine_install_dir()]
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value) / "Programs" if var == "LOCALAPPDATA" else Path(value))
    return roots


def find_windows_app_exe(dir_hints: tuple[str, ...], exe_hints: tuple[str, ...]) -> Path | None:
    """Locate an electron-builder/NSIS style install: a folder under
    %LOCALAPPDATA%\\Programs (or Program Files) whose name starts with one
    of `dir_hints`, containing an .exe whose name starts with one of
    `exe_hints`. Case-insensitive on both, so "LM Studio" and "lm-studio"
    both match. Returns None rather than guessing."""
    dir_hints_l = tuple(h.lower() for h in dir_hints)
    exe_hints_l = tuple(h.lower() for h in exe_hints)
    for root in _windows_programs_roots():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                is_matching_dir = entry.is_dir() and entry.name.lower().startswith(
                    dir_hints_l
                )
            except OSError:
                continue
            if not is_matching_dir:
                continue
            try:
                files = list(entry.iterdir())
            except OSError:
                continue
            for candidate in files:
                try:
                    if (
                        candidate.is_file()
                        and candidate.suffix.lower() == ".exe"
                        and candidate.name.lower().startswith(exe_hints_l)
                    ):
                        return candidate
                except OSError:
                    continue
    return None


def _spawn_detached(args: list[str], cwd: Path | None = None) -> None:
    """Start `args` as its own process group so it outlives omm and
    does not share omm's console (Windows) - the app keeps running
    after `omm run` returns."""
    kwargs: dict = {
        "cwd": str(cwd) if cwd else None,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def _open_gui_app(
    label: str,
    *,
    mac_app: str,
    win_dir_hints: tuple[str, ...],
    win_exe_hints: tuple[str, ...],
    linux_commands: tuple[str, ...],
    flatpak_id: str | None = None,
) -> LaunchResult:
    system = platform.system()
    try:
        if system == "Darwin":
            completed = subprocess.run(["open", "-a", mac_app], capture_output=True, timeout=15)
            if completed.returncode == 0:
                return LaunchResult(True, f"Opened {label}.")
            return LaunchResult(False, f"macOS could not open {mac_app}.app - is {label} installed?")
        if system == "Windows":
            exe = find_windows_app_exe(win_dir_hints, win_exe_hints)
            if exe is None:
                return LaunchResult(
                    False,
                    f"Could not find the {label} executable under your Programs folders. "
                    f"Open {label} yourself - the model is already in its local models list.",
                )
            _spawn_detached([str(exe)])
            return LaunchResult(True, f"Opened {label}.")
        for command in linux_commands:
            found = shutil.which(command)
            if found:
                _spawn_detached([found])
                return LaunchResult(True, f"Opened {label}.")
        if flatpak_id and shutil.which("flatpak"):
            _spawn_detached(["flatpak", "run", flatpak_id])
            return LaunchResult(True, f"Opened {label} (flatpak).")
    except (OSError, subprocess.SubprocessError) as error:
        return LaunchResult(False, f"Could not start {label}: {error}")
    return LaunchResult(
        False,
        f"No launch command for {label} is known on {system}. "
        f"Open {label} yourself - the model is already in its local models list.",
    )


# --- Per-engine launchers ----------------------------------------------------


def launch_ollama(tag: str) -> LaunchResult:
    """Hand the terminal to `ollama run <tag>` and return once the user
    leaves the chat (`/bye` or Ctrl+D). The caller is responsible for
    the daemon being up (cli._ensure_ollama_running)."""
    executable = linker.find_ollama_executable()
    if executable is None:
        return LaunchResult(False, "Ollama is not installed. Install it from https://ollama.com/download.")
    try:
        returncode = subprocess.call([str(executable), "run", tag], stdin=sys.stdin)
    except KeyboardInterrupt:
        returncode = 0
    except OSError as error:
        return LaunchResult(False, f"Could not start `ollama run {tag}`: {error}")
    if returncode != 0:
        return LaunchResult(
            False,
            f"`ollama run {tag}` exited with code {returncode}. "
            f"Try `omm link --engine ollama` to repair the model's Ollama link.",
            interactive=True,
        )
    return LaunchResult(True, "Chat ended.", interactive=True)


def launch_koboldcpp(model_path: Path) -> LaunchResult:
    binary = linker.find_koboldcpp_binary()
    if binary is None:
        return LaunchResult(False, "KoboldCpp binary not found.")
    if not model_path.is_file():
        return LaunchResult(False, f"Model file is missing: {model_path}")
    try:
        _spawn_detached([str(binary), "--model", str(model_path), "--launch"], cwd=binary.parent)
    except (OSError, subprocess.SubprocessError) as error:
        return LaunchResult(False, f"Could not start KoboldCpp: {error}")
    return LaunchResult(
        True,
        f"Started KoboldCpp with {model_path.name}; its chat UI opens at {KOBOLDCPP_DEFAULT_URL} "
        "once the model has loaded.",
    )


_TEXTGEN_START_SCRIPTS = {
    "Windows": "start_windows.bat",
    "Darwin": "start_macos.sh",
    "Linux": "start_linux.sh",
}


def launch_textgenwebui(model_filename: str) -> LaunchResult:
    root = linker.find_textgenwebui_root()
    if root is None:
        return LaunchResult(False, "text-generation-webui install not found.")
    script = root / _TEXTGEN_START_SCRIPTS.get(platform.system(), "")
    if not script.is_file():
        return LaunchResult(
            False,
            f"No start script found in {root}. Start text-generation-webui yourself and pick "
            f"{model_filename} from its Model tab.",
        )
    # Bind the value with `=` so a legitimate filename beginning with `-`
    # cannot be reinterpreted as another text-generation-webui option.
    args = [str(script), f"--model={model_filename}"]
    if platform.system() != "Windows":
        args = ["bash", *args]
    try:
        _spawn_detached(args, cwd=root)
    except (OSError, subprocess.SubprocessError) as error:
        return LaunchResult(False, f"Could not start text-generation-webui: {error}")
    return LaunchResult(
        True,
        f"Started text-generation-webui with {model_filename}; its UI opens at "
        "http://localhost:7860 once the model has loaded.",
    )


def launch_lmstudio() -> LaunchResult:
    return _open_gui_app(
        "LM Studio",
        mac_app="LM Studio",
        win_dir_hints=("lm studio", "lm-studio", "lmstudio"),
        win_exe_hints=("lm studio", "lm-studio", "lmstudio"),
        linux_commands=("lm-studio", "lmstudio"),
    )


def launch_jan() -> LaunchResult:
    return _open_gui_app(
        "Jan",
        mac_app="Jan",
        win_dir_hints=("jan",),
        win_exe_hints=("jan",),
        linux_commands=("jan",),
        flatpak_id="ai.jan.Jan",
    )


def launch_anythingllm() -> LaunchResult:
    return _open_gui_app(
        "AnythingLLM",
        mac_app="AnythingLLM",
        win_dir_hints=("anythingllm",),
        win_exe_hints=("anythingllm",),
        linux_commands=("anythingllm",),
    )


def launch_mstystudio() -> LaunchResult:
    return _open_gui_app(
        "Msty",
        mac_app="Msty Studio",
        win_dir_hints=("msty",),
        win_exe_hints=("msty",),
        linux_commands=("msty-studio", "msty"),
    )


def launch(engine: str, *, model_filename: str, model_path: Path, ollama_tag: str) -> LaunchResult:
    """Dispatch to the engine's launcher. Same if/elif shape as
    linker.is_engine_installed so monkeypatched `launch_<engine>`
    functions still take effect in tests."""
    if engine == "ollama":
        return launch_ollama(ollama_tag)
    if engine == "koboldcpp":
        return launch_koboldcpp(model_path)
    if engine == "textgenwebui":
        return launch_textgenwebui(model_filename)
    if engine == "lmstudio":
        return launch_lmstudio()
    if engine == "jan":
        return launch_jan()
    if engine == "anythingllm":
        return launch_anythingllm()
    if engine == "mstystudio":
        return launch_mstystudio()
    raise ValueError(f"unknown engine: {engine}")


def launch_description(engine: str) -> str:
    """One-line "what will happen" text for `omm run`'s status line."""
    if engine == "ollama":
        return "interactive chat in this terminal"
    if engine in ("koboldcpp", "textgenwebui"):
        return "starts with the model loaded, chat in your browser"
    return "opens the app; pick the model from its local models list"
