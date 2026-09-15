"""Per-OS background-service registration for `omm _auto-import-run`
(see watch.py). install()/uninstall() only ever touch the one per-user
service definition this feature owns; nothing here needs admin/root."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from omm.config import OMM_HOME

_LAUNCHD_LABEL = "com.omm.autoimport"
_SYSTEMD_UNIT_NAME = "omm-auto-import.service"
_SCHTASKS_NAME = "ommAutoImport"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _launchd_plist_content() -> str:
    log_path = OMM_HOME / "logs" / "auto-import.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>omm.cli</string>
        <string>_auto-import-run</string>
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
    return f"""[Unit]
Description=omm auto-import watcher

[Service]
ExecStart={sys.executable} -m omm.cli _auto-import-run
Restart=on-failure

[Install]
WantedBy=default.target
"""


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
        command = f'"{sys.executable}" -m omm.cli _auto-import-run'
        subprocess.run(
            [
                "schtasks", "/Create", "/TN", _SCHTASKS_NAME, "/TR", command,
                "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
            ],
            check=True,
        )
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
        subprocess.run(["schtasks", "/Delete", "/TN", _SCHTASKS_NAME, "/F"], check=False)
    else:
        raise RuntimeError(f"auto-import is not supported on {system}")


def is_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _launchd_plist_path().exists()
    if system == "Linux":
        return _systemd_unit_path().exists()
    if system == "Windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", _SCHTASKS_NAME], capture_output=True
        )
        return result.returncode == 0
    return False
