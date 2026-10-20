"""Optional sign-in auto-start for the local web manager (`omm web`).

Off by default; only `omm setting web-autostart enable` registers anything.
The registered command is plain `omm web` - it never opens a browser, so
signing in does not steal focus. `omm web --open` later reuses the running
manager (see `omm.web.server.serve`).

Same per-OS shape as watch_service.py (auto-import), but every OS entry has
its own name so enabling/disabling one never touches the other:
- macOS: ~/Library/LaunchAgents/com.omm.web.plist (launchd, per user).
- Linux: ~/.config/systemd/user/omm-web.service (systemd --user).
- Windows: HKCU\\...\\Run value "ommWeb". Task Scheduler ONLOGON needs an
  elevated shell, the Run key does not.
No admin/root is needed anywhere and nothing leaves the computer."""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

from omm import config as config_mod

LAUNCHD_LABEL = "com.omm.web"
SYSTEMD_UNIT_NAME = "omm-web.service"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "ommWeb"


def service_argv() -> list[str]:
    """`omm web` from the interpreter/executable omm is running in. A frozen
    build (npm, winget, portable) is itself omm, so the subcommand is passed
    to it directly instead of `-m omm.cli`."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "web"]
    return [sys.executable, "-m", "omm.cli", "web"]


def _log_path() -> Path:
    return config_mod.OMM_HOME / "logs" / "web-autostart.log"


# --- macOS ----------------------------------------------------------------


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_wrapper_path() -> Path:
    """Named shim launchd execs, so Background Items shows "omm" rather than
    "python". Kept in its own directory: auto-import's disable deletes
    OMM_HOME/bin/omm, which must not break this entry."""
    return config_mod.OMM_HOME / "bin" / "web-autostart" / "omm"


def _launchd_wrapper_content() -> str:
    if getattr(sys, "frozen", False):
        return f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    return f'#!/bin/sh\nexec "{sys.executable}" -m omm.cli "$@"\n'


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _launchd_plist_content() -> str:
    # KeepAlive only on an unsuccessful exit: `omm web` returns 0 when a
    # manager is already running (the user started one by hand), and that
    # must not respawn every ThrottleInterval seconds.
    log_path = _xml_escape(str(_log_path()))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_xml_escape(str(launchd_wrapper_path()))}</string>
        <string>web</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


# --- Linux ----------------------------------------------------------------


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _systemd_unit_content() -> str:
    return f"""[Unit]
Description=omm local web manager (omm web)

[Service]
ExecStart={shlex.join(service_argv())}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


# --- Windows --------------------------------------------------------------


def windows_service_argv() -> list[str]:
    """service_argv() through pythonw.exe when available so sign-in does not
    leave a console window open for the manager's whole lifetime."""
    argv = service_argv()
    if getattr(sys, "frozen", False):
        return argv
    executable = Path(argv[0])
    if executable.name.lower() == "python.exe":
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            argv[0] = str(windowless)
    return argv


def windows_command() -> str:
    return subprocess.list2cmdline(windows_service_argv())


def windows_run_value_read() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return None
    return str(value)


def windows_run_value_write(command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, command)


def windows_run_value_delete() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        pass


def _start_detached(argv: list[str]) -> None:
    """Start now - the Run value alone only takes effect at next sign-in."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


# Stops only a process whose command line is exactly the registered one, so a
# manager the user started by hand (`omm web` in a terminal) keeps running.
# The command travels in an environment variable, never spliced into script
# text, so paths with quotes or spaces cannot change what the script does.
_WINDOWS_STOP_SCRIPT = (
    "$target = $env:OMM_WEB_AUTOSTART_COMMAND;"
    " Get-CimInstance Win32_Process"
    " | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Trim() -eq $target }"
    " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)


def _windows_stop_started_manager(command: str) -> None:
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_STOP_SCRIPT],
            check=False,
            capture_output=True,
            timeout=30,
            env={**os.environ, "OMM_WEB_AUTOSTART_COMMAND": command},
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# --- public API -----------------------------------------------------------


def registration_path() -> str | None:
    """Where the registration lives on this OS, for `status` output."""
    system = platform.system()
    if system == "Darwin":
        return str(launchd_plist_path())
    if system == "Linux":
        return str(systemd_unit_path())
    if system == "Windows":
        return f"HKCU\\{WINDOWS_RUN_KEY}\\{WINDOWS_RUN_VALUE}"
    return None


def install() -> None:
    """Register sign-in auto-start and start the manager now (no browser).
    A failure leaves nothing registered so the next enable is a real retry."""
    system = platform.system()
    if system == "Darwin":
        wrapper = launchd_wrapper_path()
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(_launchd_wrapper_content(), encoding="utf-8")
        wrapper.chmod(0o755)
        plist = launchd_plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        _log_path().parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(_launchd_plist_content(), encoding="utf-8")
        try:
            subprocess.run(["launchctl", "load", "-w", str(plist)], check=True)
        except (OSError, subprocess.CalledProcessError):
            plist.unlink(missing_ok=True)
            wrapper.unlink(missing_ok=True)
            raise
    elif system == "Linux":
        unit = systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_systemd_unit_content(), encoding="utf-8")
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME], check=True
            )
        except (OSError, subprocess.CalledProcessError):
            unit.unlink(missing_ok=True)
            raise
    elif system == "Windows":
        argv = windows_service_argv()
        windows_run_value_write(subprocess.list2cmdline(argv))
        try:
            _start_detached(argv)
        except OSError:
            windows_run_value_delete()
            raise
    else:
        raise RuntimeError(f"web auto-start is not supported on {system}")


def uninstall() -> None:
    """Remove the registration and stop the manager this feature started.
    Models, settings and chat records are untouched."""
    system = platform.system()
    if system == "Darwin":
        plist = launchd_plist_path()
        if plist.exists():
            subprocess.run(["launchctl", "unload", "-w", str(plist)], check=False)
            plist.unlink()
        launchd_wrapper_path().unlink(missing_ok=True)
    elif system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME], check=False
        )
        systemd_unit_path().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    elif system == "Windows":
        registered = windows_run_value_read()
        windows_run_value_delete()
        _windows_stop_started_manager(registered or windows_command())
    else:
        raise RuntimeError(f"web auto-start is not supported on {system}")


def is_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return launchd_plist_path().exists()
    if system == "Linux":
        return systemd_unit_path().exists()
    if system == "Windows":
        return windows_run_value_read() is not None
    return False
