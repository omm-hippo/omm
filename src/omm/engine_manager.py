"""Read engine state and use a positively identified package manager for changes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
from typing import Callable

from omm import linker
from omm.engine_packages import PACKAGES, operation_lock
from omm.engines.lmstudio import LMStudioAdapter
from omm.engines.ollama import OllamaAdapter


class EngineManagementError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageReceipt:
    manager: str
    executable: str
    package_id: str
    version: str | None
    scope: str | None = None
    kind: str | None = None


def _environment(*, read_only: bool = True) -> dict[str, str]:
    env = {**os.environ, "HOMEBREW_NO_ANALYTICS": "1",
           "HOMEBREW_NO_INSTALL_CLEANUP": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
    if read_only:
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    return env


def _query(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15, env=_environment())
    except (OSError, subprocess.TimeoutExpired):
        return None


_VERSION_TOKEN_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def _ollama_cli_version() -> str | None:
    """`ollama --version` (#368): reports the installed binary's version
    without starting the daemon - so it works even when the local API is
    off, unlike `runtime_version`. LM Studio's `lms` CLI has no equivalent:
    `lms --version` only prints the `lms` tool's own build commit, not the
    LM Studio app version, so there's nothing safe to surface from it."""
    binary = shutil.which("ollama")
    if binary is None:
        return None
    result = _query([binary, "--version"])
    if result is None or result.returncode != 0:
        return None
    match = _VERSION_TOKEN_RE.search(result.stdout)
    return match.group(1) if match else None


def _macos_bundle_version(key: str) -> str | None:
    """The app's own Info.plist CFBundleShortVersionString (#368) - ground
    truth for "what's actually installed", and needs no new dependency
    (plistlib is stdlib). Works for any engine with a native macOS app
    bundle (ollama/lmstudio/jan/anythingllm/mstystudio); None elsewhere,
    including koboldcpp/textgenwebui which have no bundle at all."""
    path = linker.engine_app_bundle_path(key)
    if path is None:
        return None
    try:
        with (path / "Contents" / "Info.plist").open("rb") as plist_file:
            data = plistlib.load(plist_file)
    except (OSError, plistlib.InvalidFileException):
        return None
    version = data.get("CFBundleShortVersionString")
    return version if isinstance(version, str) and version else None


# Codepage/language pairs to try when an exe's VarFileInfo\Translation
# table is missing: US English + Unicode / Windows-1252, then language-neutral.
_FALLBACK_VERSION_TRANSLATIONS = ("040904b0", "040904e4", "000004b0")


def _load_version_dll():
    """Seam for tests: the real version.dll, only ever loaded on Windows."""
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("version")
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    dll.GetFileVersionInfoW.restype = wintypes.BOOL
    dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
    ]
    dll.VerQueryValueW.restype = wintypes.BOOL
    return dll


def _read_exe_version(path) -> str | None:
    """An .exe's own version resource - `ProductVersion`, falling back to
    `FileVersion` - the same string Explorer's Details tab and PowerShell's
    `(Get-Item x.exe).VersionInfo.ProductVersion` show (#368). stdlib ctypes
    + version.dll, no new dependency. None off Windows, for an exe with no
    version resource, and on any ctypes/OS error: this is display only."""
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        dll = _load_version_dll()
        path_str = str(path)
        size = dll.GetFileVersionInfoSizeW(path_str, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(path_str, 0, size, data):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        translations = []
        if (dll.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length))
                and pointer.value and length.value >= 4):
            words = (ctypes.c_ushort * (length.value // 2)).from_address(pointer.value)
            translations = [f"{words[i]:04x}{words[i + 1]:04x}" for i in range(0, len(words) - 1, 2)]
        translations += [t for t in _FALLBACK_VERSION_TRANSLATIONS if t not in translations]
        for field in ("ProductVersion", "FileVersion"):
            for translation in translations:
                pointer = ctypes.c_void_p()
                length = ctypes.c_uint()
                if not dll.VerQueryValueW(
                    data, f"\\StringFileInfo\\{translation}\\{field}",
                    ctypes.byref(pointer), ctypes.byref(length),
                ) or not pointer.value or not length.value:
                    continue
                value = ctypes.wstring_at(pointer.value, length.value).split("\x00", 1)[0].strip()
                if value:
                    return value
    except Exception:  # noqa: BLE001 - any ctypes/loader failure means "unknown", never a crash
        return None
    return None


def _windows_exe_version(key: str) -> str | None:
    """Windows counterpart of _macos_bundle_version (#368): the installed
    main .exe's own version resource. Works whether or not the app is
    running; None for koboldcpp/textgenwebui and anything not found."""
    path = linker.engine_windows_executable(key)
    if path is None:
        return None
    return _read_exe_version(path)


def _detected_version(key: str) -> str | None:
    """Best-effort version for an engine `package_receipt()` couldn't
    identify (#368): the installed app's own metadata first - macOS
    Info.plist or the Windows exe version resource, both work whether or
    not it's running - then a CLI --version flag where one exists and is
    trustworthy. Display only - never touches
    `PackageReceipt`/update/uninstall command assembly."""
    version = _macos_bundle_version(key)
    if version is None:
        version = _windows_exe_version(key)
    if version is None and key == "ollama":
        version = _ollama_cli_version()
    return version


def package_receipt(key: str) -> PackageReceipt | None:
    package = PACKAGES[key]
    system = platform.system()
    if system == "Darwin" and (package.brew_cask or package.brew_formula) and (binary := shutil.which("brew")):
        found = []
        for kind, package_id in (("cask", package.brew_cask), ("formula", package.brew_formula)):
            if package_id is None:
                continue
            result = _query([binary, "list", f"--{kind}", "--versions", package_id])
            if result is None:
                raise EngineManagementError(f"Could not read Homebrew's installed {kind} packages.")
            if result.returncode != 0:
                continue
            matches = [line.split() for line in result.stdout.splitlines()
                       if line.split() and line.split()[0] == package_id]
            if len(matches) == 1 and len(matches[0]) > 1:
                found.append(PackageReceipt("brew", binary, package_id, " ".join(matches[0][1:]), kind=kind))
            elif matches:
                raise EngineManagementError("Homebrew returned ambiguous package information.")
        if len(found) > 1:
            raise EngineManagementError("Both Ollama app and formula are installed; manage the intended package explicitly with Homebrew.")
        return found[0] if found else None
    elif system == "Windows" and package.winget_id and (binary := shutil.which("winget")):
        result = _query([binary, "list", "--id", package.winget_id, "--exact",
                         "--source", "winget", "--disable-interactivity"])
        if result is None:
            raise EngineManagementError("WinGet package lookup did not finish.")
        if result.returncode != 0:
            # APPINSTALLER_CLI_ERROR_NO_APPLICATIONS_FOUND, not a generic failure.
            if result.returncode & 0xFFFFFFFF == 0x8A150014:
                return None
            raise EngineManagementError(f"WinGet package lookup failed ({result.returncode}).")
        if result and result.returncode == 0:
            # Match the complete ID, not translated headers or a truncated name.
            pattern = re.compile(r"^.+?\s+" + re.escape(package.winget_id) + r"\s+(\S+)(?:\s|$)")
            matches = [match.group(1) for line in result.stdout.splitlines()
                       if (match := pattern.match(line.strip()))]
            if len(matches) == 1:
                return PackageReceipt("winget", binary, package.winget_id, matches[0])
            raise EngineManagementError("WinGet did not return one exact, readable package identity.")
    elif system == "Linux" and package.flatpak_id and (binary := shutil.which("flatpak")):
        found = []
        result = _query([binary, "list", "--app", "--columns=application,version,installation"])
        if result is None or result.returncode != 0:
            raise EngineManagementError("Could not read Flatpak's installed applications.")
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == package.flatpak_id:
                if fields[-1] not in {"user", "system"}:
                    raise EngineManagementError("This Flatpak uses a custom installation; manage it explicitly with Flatpak.")
                found.append(PackageReceipt("flatpak", binary, package.flatpak_id,
                                             fields[1] if len(fields) >= 3 else None, fields[-1]))
        # Never select one arbitrarily when both installations exist.
        if len(found) > 1:
            raise EngineManagementError("Multiple Flatpak installations were found; select one with Flatpak itself.")
        return found[0] if found else None
    return None


def inspect_engine(key: str, *, check_api: bool = True) -> dict:
    package = PACKAGES[key]
    installed = linker.is_engine_installed(key)
    package_error = None
    try:
        receipt = package_receipt(key)
    except EngineManagementError as error:
        receipt = None
        package_error = str(error)
    result = {
        "key": key, "label": package.label, "installed": installed,
        "package": asdict(receipt) if receipt else None,
        "package_error": package_error,
        "package_manageable": bool(package.brew_cask or package.brew_formula
                                    or package.winget_id or package.flatpak_id),
        "detected_version": _detected_version(key) if receipt is None else None,
        "api_status": "not_checked", "runtime_version": None,
        "manual_url": package.manual_url,
    }
    if check_api and key in {"ollama", "lmstudio"}:
        adapter = OllamaAdapter() if key == "ollama" else LMStudioAdapter()
        health = adapter.health()
        result["api_status"] = "ready" if health.reachable else health.failure_reason or "unreachable"
        result["runtime_version"] = health.version
    elif check_api:
        result["api_status"] = "diagnostics_unavailable"
    return result


def command_for(action: str, receipt: PackageReceipt) -> list[str]:
    if action not in {"update", "uninstall"}:
        raise ValueError("unsupported engine action")
    if receipt.manager == "brew":
        if receipt.kind not in {None, "cask", "formula"}:
            raise EngineManagementError("Unknown Homebrew package kind.")
        return [receipt.executable, "upgrade" if action == "update" else "uninstall",
                "--formula" if receipt.kind == "formula" else "--cask", receipt.package_id]
    if receipt.manager == "winget":
        command = [receipt.executable, "upgrade" if action == "update" else "uninstall",
                   "--id", receipt.package_id, "--exact", "--source", "winget",
                   "--silent", "--disable-interactivity"]
        if action == "update":
            command += ["--accept-source-agreements", "--accept-package-agreements"]
        return command
    if receipt.manager == "flatpak" and receipt.scope in {"user", "system"}:
        return [receipt.executable, "update" if action == "update" else "uninstall",
                f"--{receipt.scope}", "--noninteractive", "-y", receipt.package_id]
    raise EngineManagementError("Unsupported or ambiguous package manager.")


def plan_action(key: str, action: str) -> dict:
    receipt = package_receipt(key)
    if receipt is None:
        raise EngineManagementError(
            f"Cannot identify one installed package for {PACKAGES[key].label}. "
            f"Use its own installer/settings: {PACKAGES[key].manual_url}"
        )
    return {"engine": key, "action": action, "package": asdict(receipt),
            "command": command_for(action, receipt), "omm_models_preserved": True}


def _run_action(command: list[str], on_output: Callable[[str], None]) -> int:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=_environment(read_only=False))
    try:
        for line in proc.stdout:
            on_output(line.rstrip("\r\n"))
        return proc.wait()
    except BaseException:
        # Only terminate the process we started; never kill the runner app.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def execute_action(plan: dict, *, on_output: Callable[[str], None]) -> dict:
    key, action = plan["engine"], plan["action"]
    with operation_lock(key):
        fresh = plan_action(key, action)
        if fresh != plan:
            raise EngineManagementError("The installed package changed; inspect it again before retrying.")
        started = time.monotonic()
        on_output(f"Running {action} through {plan['package']['manager']}...")
        code = _run_action(fresh["command"], on_output)
        on_output("Checking the installed package after the operation...")
        receipt = package_receipt(key)
        installed = linker.is_engine_installed(key)
        no_update = (action == "update" and plan["package"]["manager"] == "winget"
                     and code & 0xFFFFFFFF == 0x8A15002B)
        if code != 0 and not no_update:
            raise EngineManagementError(
                f"Package manager exited with status {code}. "
                "State was rechecked; run `omm engine status` before retrying."
            )
        if action == "uninstall" and receipt is not None:
            raise EngineManagementError("Package manager returned success but the package is still installed.")
        if action == "update" and (receipt is None or not installed):
            raise EngineManagementError("Package manager returned success but the updated engine could not be verified.")
        return {
            "engine": key, "action": action,
            "status": "unchanged" if action == "update" and asdict(receipt) == plan["package"] else "completed",
            "installed": installed, "package": asdict(receipt) if receipt else None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "omm_models_preserved": True,
        }
