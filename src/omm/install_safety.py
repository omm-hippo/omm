"""Mandatory, bounded checks around OMM-managed runner install/update.

Passing these named checks is not a claim of zero vulnerabilities. Never
changes native protection settings, removes threats, or launches the target.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from omm import config
from omm.atomic import atomic_write_text
from omm.engine_packages import PACKAGES

POLICY_VERSION = 1
MAX_SCAN_SECONDS = 300


class SafetyError(RuntimeError):
    pass


def last_result(key):
    if key not in PACKAGES:
        raise ValueError("Unknown engine")
    path = config.OMM_HOME / "security-checks" / (key + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("policy_version") != POLICY_VERSION or
                value.get("engine") != key or value.get("status") not in {"named_checks_passed", "failed"}):
            return None
        return {k: value[k] for k in ("policy_version", "engine", "platform", "checked_at", "status", "checks",
                                      "all_vulnerabilities_assessed") if k in value}
    except (OSError, ValueError):
        return None


def record_failure(key):
    """Keep a failed postcheck visible; never copy native error bodies."""
    try:
        atomic_write_text(config.OMM_HOME / "security-checks" / (key + ".json"), json.dumps({
            "policy_version": POLICY_VERSION, "engine": key, "platform": platform.system(),
            "checked_at": datetime.now(timezone.utc).isoformat(), "status": "failed", "checks": [],
            "all_vulnerabilities_assessed": False}))
    except OSError:
        pass


def environment():
    return {**os.environ, "LANG": "C", "LC_ALL": "C",
            "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1",
            "HOMEBREW_NO_INSTALL_CLEANUP": "1"}


def _run(args, *, timeout=30, env=None, input=None):
    name = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    label = {"codesign": "macOS signature", "spctl": "Gatekeeper", "clamscan": "ClamAV",
             "powershell.exe": "Windows signature/protection", "mpcmdrun.exe": "Defender scan",
             "brew": "Homebrew metadata", "flatpak": "Flatpak source"}.get(name, "security tool")
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, env=env or environment(), input=input)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SafetyError(f"Mandatory {label} check could not run or timed out. Installation is not verified.") from error
    if result.returncode != 0:
        raise SafetyError(f"Mandatory {label} check failed (exit {result.returncode}). Keep OS protections enabled and inspect the package before retrying.")
    return result.stdout


def _json(args, **kwargs):
    try:
        value = json.loads(_run(args, **kwargs))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, TypeError) as error:
        raise SafetyError("Mandatory security check returned unrecognized data; installation is blocked.") from error


def _reject_bypasses(command):
    blocked = {"--no-quarantine", "--ignore-security-hash", "--ignore-local-archive-malware-scan",
               "--no-gpg-verify", "--insecure", "--ignore-invalid-hash"}
    if any(arg.split("=", 1)[0] in blocked for arg in command):
        raise SafetyError("Security verification bypass flags are not allowed.")
    for name in ("HOMEBREW_NO_VERIFY_ATTESTATIONS", "HOMEBREW_CURLRC", "HOMEBREW_CURL_INSECURE",
                 "HOMEBREW_API_DOMAIN", "HOMEBREW_BOTTLE_DOMAIN", "HOMEBREW_ARTIFACT_DOMAIN",
                 "HOMEBREW_CASK_GIT_REMOTE", "HOMEBREW_CORE_GIT_REMOTE"):
        if os.environ.get(name):
            raise SafetyError("A package source or verification override is set. Clear it before managed installation.")
    options = os.environ.get("HOMEBREW_CASK_OPTS", "")
    if "--no-quarantine" in options or "--insecure" in options:
        raise SafetyError("Homebrew quarantine/integrity bypass settings are not allowed.")


def _defender():
    script = "$ErrorActionPreference='Stop'; Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,AntivirusSignatureAge | ConvertTo-Json -Compress"
    data = _json(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    if any(data.get(k) is not True for k in ("AMServiceEnabled", "AntivirusEnabled", "RealTimeProtectionEnabled")):
        raise SafetyError("Microsoft Defender must be active with real-time protection; no setting was changed.")
    age = data.get("AntivirusSignatureAge")
    if type(age) is not int or not 0 <= age <= 7:
        raise SafetyError("Defender signatures are missing or older than seven days. Update them before installation.")


def prepare(key, command):
    """Return a hardened command only after the mandatory preflight passes."""
    if key not in PACKAGES or not isinstance(command, (list, tuple)) or len(command) < 2 or any(not isinstance(a, str) for a in command):
        raise SafetyError("Unknown software installation route.")
    command = list(command)
    _reject_bypasses(command)
    package = PACKAGES[key]
    manager = command[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    system = platform.system()
    if system == "Darwin" and manager == "brew":
        if "--cask" not in command or package.brew_cask not in command:
            raise SafetyError("Only the reviewed cask route has native application verification. Use its app installer route.")
        if "assessments enabled" != _run(["/usr/sbin/spctl", "--status"]).strip():
            raise SafetyError("Gatekeeper assessment must be enabled; OMM will not change it.")
        data = _json([command[0], "info", "--json=v2", "--cask", "homebrew/cask/" + package.brew_cask])
        rows = data.get("casks")
        if not isinstance(rows, list) or len(rows) != 1:
            raise SafetyError("Homebrew did not identify exactly one reviewed cask.")
        row = rows[0]
        if (not isinstance(row, dict) or row.get("token") != package.brew_cask or row.get("tap") != "homebrew/cask" or
                not re.fullmatch(r"[0-9a-fA-F]{64}", str(row.get("sha256", ""))) or
                not str(row.get("url", "")).startswith("https://") or
                row.get("disabled") or row.get("deprecated") or row.get("version") in (None, "latest")):
            raise SafetyError("Official fixed-version cask with HTTPS and SHA-256 is required; unknown/deprecated packages are blocked.")
        command = ["homebrew/cask/" + arg if arg == package.brew_cask else arg for arg in command]
        command.extend(flag for flag in ("--require-sha", "--quarantine") if flag not in command)
    elif system == "Windows" and manager == "winget":
        if not package.winget_id or package.winget_id not in command or "--source" not in command or command[command.index("--source") + 1:] [:1] != ["winget"]:
            raise SafetyError("The exact reviewed WinGet package and official source are required.")
        data = _json([command[0], "source", "export", "--name", "winget"])
        arg = data.get("Arg", data.get("Argument"))
        if (arg != "https://cdn.winget.microsoft.com/cache" or data.get("Type") != "Microsoft.PreIndexed.Package" or
                data.get("Name") != "winget" or data.get("Identifier") != "Microsoft.Winget.Source_8wekyb3d8bbwe"):
            raise SafetyError("WinGet's winget source is not the official Microsoft source.")
        _defender()
    elif system == "Linux" and manager == "flatpak":
        if not package.flatpak_id or package.flatpak_id not in command:
            raise SafetyError("The exact reviewed Flatpak application and Flathub source are required.")
        if "flathub" not in command and (command[1] != "update" or
                _run([command[0], "info", "--show-origin", package.flatpak_id]).strip() != "flathub"):
            raise SafetyError("The installed Flatpak must originate from Flathub.")
        rows = [line.split("\t") for line in _run([command[0], "remotes", "--columns=name,url,options"]).splitlines()]
        sources = [row for row in rows if row and row[0] == "flathub"]
        if (len(sources) != 1 or len(sources[0]) != 3 or
                sources[0][1].rstrip("/") not in {"https://dl.flathub.org/repo", "https://flathub.org/repo"} or
                "no-gpg-verify" in sources[0][2]):
            raise SafetyError("One official Flathub source with GPG verification is required.")
        if shutil.which("clamscan") is None:
            raise SafetyError("ClamAV clamscan with current official signatures is required before Linux managed installation.")
        # Loading the scanner/database here prevents deployment when unusable.
        _run(["clamscan", "--official-db-only=yes", "--fail-if-cvd-older-than=7", "-"], input="", timeout=60)
    else:
        raise SafetyError("No verified mandatory security-check route exists for this software/platform.")
    return command


def verify_installed(key):
    """Check the actual installed target before OMM reports installation success."""
    from omm import linker
    system = platform.system()
    checks = []
    if system == "Darwin":
        target = linker.engine_app_bundle_path(key)
        if target is None or not target.is_dir():
            raise SafetyError("Cannot find the installed application for mandatory signature and Gatekeeper checks.")
        _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)], timeout=60)
        _run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(target)], timeout=60)
        checks += ["codesign_deep_strict", "gatekeeper_assessment"]
    elif system == "Windows":
        target = linker.engine_windows_executable(key)
        if target is None or not target.is_file():
            raise SafetyError("Cannot find the installed executable for mandatory signature and malware checks.")
        env = {**environment(), "OMM_INSTALL_SECURITY_PATH": str(target)}
        script = "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:OMM_INSTALL_SECURITY_PATH; if($s.Status -ne 'Valid'){exit 1}; Write-Output 'AUTHENTICODE_VALID'"
        if _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env=env).strip() != "AUTHENTICODE_VALID":
            raise SafetyError("Authenticode signature could not be verified.")
        _defender()
        scanner = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
        if not scanner.is_file():
            raise SafetyError("Microsoft Defender custom scanner is unavailable; verification is blocked.")
        _run([str(scanner), "-Scan", "-ScanType", "3", "-File", str(target.parent), "-DisableRemediation"], timeout=MAX_SCAN_SECONDS)
        checks += ["authenticode", "defender_custom_scan_no_remediation"]
    elif system == "Linux":
        package = PACKAGES[key]
        if not package.flatpak_id:
            raise SafetyError("No reviewed installed Linux application location.")
        location = _run(["flatpak", "info", "--show-location", package.flatpak_id]).strip()
        target = Path(location)
        roots = [Path("/var/lib/flatpak/app") / package.flatpak_id,
                 Path.home() / ".local/share/flatpak/app" / package.flatpak_id]
        if not target.is_absolute() or not target.is_dir() or not any(target.resolve().is_relative_to(r.resolve()) for r in roots):
            raise SafetyError("Flatpak returned an unrecognized application location; no arbitrary directory was scanned.")
        output = _run(["clamscan", "--recursive=yes", "--official-db-only=yes", "--fail-if-cvd-older-than=7",
                       "--alert-exceeds-max=yes", "--alert-encrypted=yes", "--follow-dir-symlinks=0",
                       "--follow-file-symlinks=0", "--", str(target)], timeout=MAX_SCAN_SECONDS)
        scanned = re.search(r"Scanned files:\s*(\d+)", output)
        if not scanned or int(scanned[1]) == 0:
            raise SafetyError("Scanner did not report checking any files; installation is not verified.")
        checks += ["flatpak_gpg_source", "clamav_official_signatures_scan"]
    else:
        raise SafetyError("Native installation verification is unavailable.")
    receipt = {"policy_version": POLICY_VERSION, "engine": key, "platform": system,
               "checked_at": datetime.now(timezone.utc).isoformat(), "status": "named_checks_passed",
               "checks": checks, "all_vulnerabilities_assessed": False,
               "limits": "These checks do not prove that software has no vulnerabilities or malicious behavior."}
    atomic_write_text(config.OMM_HOME / "security-checks" / (key + ".json"), json.dumps(receipt))
    return receipt
