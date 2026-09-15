"""Per-OS background-service registration for `omm _auto-import-run`
(see watch.py). install()/uninstall() only ever touch the one per-user
service definition this feature owns; nothing here needs admin/root.

Windows uses the per-user Run registry key rather than Task Scheduler:
`schtasks /Create /SC ONLOGON` needs an elevated shell ("Access is
denied" from every ordinary PowerShell/cmd window), which made
`omm setting auto-import enable` fail for every non-admin Windows user.
HKCU\\...\\Run needs no elevation and starts the watcher at sign-in the
same way. The watcher is also started right away so enable behaves like
launchd's RunAtLoad and `systemctl --user enable --now` do."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from omm.config import OMM_HOME

_LAUNCHD_LABEL = "com.omm.autoimport"
_SYSTEMD_UNIT_NAME = "omm-auto-import.service"
_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WINDOWS_RUN_VALUE = "ommAutoImport"
# Name of the Task Scheduler entry the first Windows implementation created
# (only ever successfully from elevated shells). Still removed on disable so
# nobody is left with two watchers after upgrading.
_LEGACY_SCHTASKS_NAME = "ommAutoImport"


def service_argv() -> list[str]:
    """The command that runs the watcher, from the same interpreter omm is
    running in. The PyInstaller builds (npm, winget, portable) have no
    `omm.cli` module to `-m` into - sys.executable *is* omm there, so the
    subcommand is passed to it directly."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "_auto-import-run"]
    return [sys.executable, "-m", "omm.cli", "_auto-import-run"]


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _launchd_plist_content() -> str:
    log_path = OMM_HOME / "logs" / "auto-import.log"
    program_arguments = "\n".join(
        f"        <string>{argument}</string>" for argument in service_argv()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def _systemd_unit_content() -> str:
    exec_start = " ".join(service_argv())
    return f"""[Unit]
Description=omm auto-import watcher

[Service]
ExecStart={exec_start}
Restart=on-failure

[Install]
WantedBy=default.target
"""


# --- Windows --------------------------------------------------------------


def _windows_service_argv() -> list[str]:
    """service_argv(), but through pythonw.exe when the interpreter has one
    (every venv does, pipx's included). A Run-key command that launches
    python.exe opens a console window at every sign-in and leaves it on
    screen for as long as the watcher lives; pythonw.exe has no console."""
    argv = service_argv()
    if getattr(sys, "frozen", False):
        return argv
    executable = Path(argv[0])
    if executable.name.lower() == "python.exe":
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            argv[0] = str(windowless)
    return argv


def _windows_run_value_read() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return None
    return str(value)


def _windows_run_value_write(command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, _WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, command)


def _windows_run_value_delete() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        pass


def _start_detached(argv: list[str]) -> None:
    """Start the watcher now, with no console and outliving this command -
    the Run key alone would only take effect at the next sign-in."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


# Best-effort counterpart of `launchctl unload` / `systemctl --user disable
# --now`: stop a watcher started at sign-in or by install(). Matches only our
# own `_auto-import-run` command line and never the PowerShell running the
# query (its own command line contains the same text).
_WINDOWS_STOP_SCRIPT = (
    "Get-CimInstance Win32_Process"
    " | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*_auto-import-run*' }"
    " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)


def _windows_stop_running_watcher() -> None:
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_STOP_SCRIPT],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _windows_legacy_task_exists() -> bool:
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", _LEGACY_SCHTASKS_NAME],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _windows_legacy_task_delete() -> None:
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", _LEGACY_SCHTASKS_NAME, "/F"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# --- public API -----------------------------------------------------------


def install() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        (OMM_HOME / "logs").mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_launchd_plist_content(), encoding="utf-8")
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    elif system == "Linux":
        unit_path = _systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_systemd_unit_content(), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT_NAME], check=True
        )
    elif system == "Windows":
        argv = _windows_service_argv()
        _windows_run_value_write(subprocess.list2cmdline(argv))
        try:
            _start_detached(argv)
        except OSError:
            # Leave nothing registered behind a failed enable, so the next
            # attempt is a real retry rather than "already enabled".
            _windows_run_value_delete()
            raise
    else:
        raise RuntimeError(f"auto-import is not supported on {system}")


def uninstall() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)], check=False)
            plist_path.unlink()
    elif system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT_NAME], check=False
        )
        _systemd_unit_path().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    elif system == "Windows":
        _windows_run_value_delete()
        _windows_legacy_task_delete()
        _windows_stop_running_watcher()
    else:
        raise RuntimeError(f"auto-import is not supported on {system}")


def is_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _launchd_plist_path().exists()
    if system == "Linux":
        return _systemd_unit_path().exists()
    if system == "Windows":
        return _windows_run_value_read() is not None or _windows_legacy_task_exists()
    return False
