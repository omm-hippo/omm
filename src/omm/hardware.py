"""Cross-platform hardware scanning for RAM/VRAM/OS detection."""

from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
from dataclasses import dataclass

import psutil

RAM_MODEL_CAP_RATIO = 0.80
RAM_SAFETY_RESERVE_RATIO = 0.10
RAM_SAFETY_RESERVE_MIN_GB = 1.0
VRAM_MODEL_CAP_RATIO = 0.90
VRAM_SAFETY_RESERVE_RATIO = 0.05
VRAM_SAFETY_RESERVE_MIN_GB = 0.5
BUSY_CPU_PERCENT = 25.0
"""System-wide CPU utilization, measured while omm is idle, above which a
benchmark taken right now is treated as contaminated by background load.
Set well above a quiet desktop's baseline so routine idle chatter never
suppresses calibration."""


@dataclass
class HardwareInfo:
    os_name: str
    os_version: str
    cpu: str
    ram_total_gb: float
    ram_available_gb: float
    unified_memory: bool
    gpu_name: str | None
    vram_total_gb: float | None
    vram_free_gb: float | None
    gpu_tflops: float | None = None
    cpu_arch: str = "unknown"
    cpu_physical_cores: int = 0
    cpu_logical_cores: int = 0


@dataclass(frozen=True)
class MemoryBudget:
    """Live capacity that can be assigned without crowding other apps."""

    model_budget_gb: float
    ram_budget_gb: float
    vram_budget_gb: float | None
    ram_safety_reserve_gb: float
    vram_safety_reserve_gb: float | None
    constrained_by_live_usage: bool
    install_budget_gb: float
    """Total-RAM-based cap, ignoring current free memory. Use this for
    install/search/recommend decisions, which pick a model to keep long
    term and shouldn't be swayed by other apps' memory use at scan time."""


def available_ram_gb() -> float:
    """Return current available RAM without a full CPU/GPU hardware scan.

    Runtime pressure monitoring must be cheap enough to honor its polling
    interval. On Windows, ``scan_hardware`` can take several seconds and miss
    short but meaningful pressure events.
    """
    return psutil.virtual_memory().available / (1024**3)


def sample_cpu_utilization_percent(
    samples: int = 3, interval_seconds: float = 0.2
) -> float | None:
    """Return median system-wide CPU utilization over a short window.

    Called just before a benchmark starts, while omm itself is idle, so the
    number describes *other* programs' load. Sustained background load
    depresses every speed sample by roughly the same amount, which the
    within-run MAD/median dispersion cannot see - the run looks tight and
    reads low. This is the only signal omm has for that case.

    The median of several short windows is deliberate: a single window can
    be dominated by one momentary spike, and a spike that ends before the
    benchmark does not bias the result the way sustained load does.

    Returns None when psutil cannot report utilization, so callers treat an
    unavailable reading as "unknown", never as "idle".
    """
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 10:
        raise ValueError("samples must be an integer from 1 to 10")
    if not 0 < interval_seconds <= 5:
        raise ValueError("interval_seconds must be greater than 0 and at most 5")
    readings = []
    for _ in range(samples):
        try:
            value = psutil.cpu_percent(interval=interval_seconds)
        except (psutil.Error, OSError, ValueError):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        readings.append(max(0.0, min(100.0, float(value))))
    return statistics.median(readings)


@dataclass(frozen=True)
class WindowsCommitInfo:
    """System-wide Windows commit counters, in GiB.

    ``available_gb`` is headroom a new allocation can take right now.
    ``limit_gb`` is the whole configured budget - RAM plus the current
    pagefile - and therefore what a candidate can never exceed.
    """

    available_gb: float
    limit_gb: float


def _windows_commit_info(
    commit_total_pages: int, commit_limit_pages: int, page_size_bytes: int
) -> WindowsCommitInfo | None:
    """Convert Windows system commit counters to GiB.

    This must remain separate from ``psutil.virtual_memory().available``:
    physical pages available for reuse and pagefile-backed commit capacity are
    different budgets on Windows.
    """
    if (
        isinstance(commit_total_pages, bool)
        or isinstance(commit_limit_pages, bool)
        or isinstance(page_size_bytes, bool)
        or not all(
            isinstance(value, int)
            for value in (commit_total_pages, commit_limit_pages, page_size_bytes)
        )
        or commit_total_pages < 0
        or commit_limit_pages < commit_total_pages
        or page_size_bytes <= 0
    ):
        return None
    return WindowsCommitInfo(
        available_gb=(commit_limit_pages - commit_total_pages) * page_size_bytes / (1024**3),
        limit_gb=commit_limit_pages * page_size_bytes / (1024**3),
    )


def windows_commit_info() -> WindowsCommitInfo | None:
    """Return the current system-wide Windows commit counters.

    ``CommitLimit - CommitTotal`` is capacity newly committed pages may use.
    Return ``None`` when Windows cannot provide the counters, so callers retain
    their portable physical-memory fallback instead of assuming capacity.
    """
    if platform.system() != "Windows":
        return None

    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):
        # ``ctypes.wintypes`` defines Windows-only simple types and raises on
        # other platforms, so it must never be imported at module scope.
        return None

    class PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    try:
        info = PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(info)
        get_performance_info = ctypes.WinDLL("psapi", use_last_error=True).GetPerformanceInfo
        get_performance_info.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]
        get_performance_info.restype = wintypes.BOOL
        if not get_performance_info(ctypes.byref(info), info.cb):
            return None
        return _windows_commit_info(info.CommitTotal, info.CommitLimit, info.PageSize)
    except (AttributeError, OSError):
        return None


def calculate_memory_budget(hw: HardwareInfo) -> MemoryBudget:
    """Return a conservative model budget from current free RAM and VRAM.

    ``psutil.available`` includes reclaimable memory. Localfit still leaves a
    proportional reserve for the OS and applications opened after the scan.
    Unified-memory Macs use the RAM result once because CPU and GPU share it.
    """
    ram_reserve = max(
        RAM_SAFETY_RESERVE_MIN_GB,
        hw.ram_total_gb * RAM_SAFETY_RESERVE_RATIO,
    )
    ram_total_cap = hw.ram_total_gb * RAM_MODEL_CAP_RATIO
    ram_live_cap = max(0.0, hw.ram_available_gb - ram_reserve)
    ram_budget = min(ram_total_cap, ram_live_cap)
    ram_constrained = ram_live_cap < ram_total_cap

    if hw.unified_memory or hw.vram_total_gb is None:
        return MemoryBudget(
            model_budget_gb=ram_budget,
            ram_budget_gb=ram_budget,
            vram_budget_gb=None,
            ram_safety_reserve_gb=ram_reserve,
            vram_safety_reserve_gb=None,
            constrained_by_live_usage=ram_constrained,
            install_budget_gb=ram_total_cap,
        )

    vram_total = hw.vram_total_gb
    vram_free = hw.vram_free_gb if hw.vram_free_gb is not None else vram_total
    vram_reserve = max(
        VRAM_SAFETY_RESERVE_MIN_GB,
        vram_total * VRAM_SAFETY_RESERVE_RATIO,
    )
    vram_total_cap = vram_total * VRAM_MODEL_CAP_RATIO
    vram_live_cap = max(0.0, vram_free - vram_reserve)
    vram_budget = min(vram_total_cap, vram_live_cap)
    return MemoryBudget(
        # Backends can split layers between dedicated VRAM and RAM. Using the
        # larger safe pool is conservative and avoids double-counting memory.
        model_budget_gb=max(ram_budget, vram_budget),
        ram_budget_gb=ram_budget,
        vram_budget_gb=vram_budget,
        ram_safety_reserve_gb=ram_reserve,
        vram_safety_reserve_gb=vram_reserve,
        constrained_by_live_usage=(
            ram_constrained or vram_live_cap < vram_total_cap
        ),
        install_budget_gb=max(ram_total_cap, vram_total_cap),
    )


_OS_DISPLAY_NAMES = {"Darwin": "macOS"}


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _mac_cpu_brand() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return platform.processor() or "Unknown"


def _mac_chip_name() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Apple Silicon"


def _linux_cpu_model() -> str | None:
    """Return Linux CPU brand; platform.processor() is often only ``x86_64``."""
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8", errors="replace"):
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"model name", "hardware"}:
                model = value.strip()
                if model:
                    return model
    except OSError:
        pass
    return None


# Windows PowerShell 5.1 encodes redirected stdout with `[Console]::OutputEncoding`,
# which defaults to the *console* output code page it inherited - 949 in a Korean
# console, 437 in a US OEM one, 65001 only if something ran `chcp 65001` first. So
# the bytes on the pipe are not UTF-8 by default and are not even stable across
# machines. Worse, at a code page that cannot represent the text (437 and a Korean
# device name) PowerShell substitutes "?" *before* writing, destroying the value
# where no decoding choice on our side could recover it.
#
# Pinning `[Console]::OutputEncoding` inside the child, before it emits anything,
# makes the pipe genuinely UTF-8 in every one of those cases, which is what the
# `encoding="utf-8"` below then decodes correctly.
_PS_FORCE_UTF8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "

# ...but that assignment is not process-local: .NET implements it with
# SetConsoleOutputCP, which mutates the console the child *shares with us*.
# Measured on this repo's Korean Windows box: console code page 949 before the
# call, 65001 after, i.e. the user's terminal is left switched for every command
# they run once omm exits. CREATE_NO_WINDOW gives the child its own (hidden)
# console instead, so the pin applies to that one and the caller's is untouched
# - verified to stay at 949/437/65001 across the same matrix. It also stops a
# console window flashing up when omm is launched from a GUI shell.
# See tests/test_windows_cim_encoding.py for the live checks.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _powershell_json(command: str, *, timeout: float = 5):
    """Run a PowerShell snippet that prints JSON and parse it, or return None."""
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    try:
        result = subprocess.run(
            [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", _PS_FORCE_UTF8 + command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=True,
            creationflags=_NO_WINDOW,
        )
        return json.loads(result.stdout.lstrip("\ufeff"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _windows_cim(class_name: str, properties: list[str]) -> list[dict]:
    """Read a small CIM projection without depending on pywin32/wmi."""
    projection = ",".join(properties)
    data = _powershell_json(
        f"Get-CimInstance {class_name} | Select-Object {projection} | "
        "ConvertTo-Json -Compress"
    )
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _windows_cpu_model() -> str | None:
    for item in _windows_cim("Win32_Processor", ["Name"]):
        name = str(item.get("Name") or "").strip()
        if name:
            return " ".join(name.split())
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            if name:
                return " ".join(name.split())
    except (ImportError, OSError):
        pass
    return None


def _windows_registry_gpus() -> list[dict]:
    """Registry fallback for locked-down machines where CIM is denied."""
    try:
        import winreg
    except ImportError:
        return []
    base_path = r"SYSTEM\CurrentControlSet\Control\Video"
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base:
            guid_count = winreg.QueryInfoKey(base)[0]
            for guid_index in range(guid_count):
                guid = winreg.EnumKey(base, guid_index)
                try:
                    with winreg.OpenKey(base, guid) as adapter:
                        child_count = winreg.QueryInfoKey(adapter)[0]
                        children = [winreg.EnumKey(adapter, index) for index in range(child_count)]
                except OSError:
                    continue
                for child in children:
                    try:
                        with winreg.OpenKey(base, f"{guid}\\{child}") as key:
                            values = {}
                            for field in (
                                "DriverDesc",
                                "HardwareInformation.AdapterString",
                                "HardwareInformation.qwMemorySize",
                            ):
                                try:
                                    values[field] = winreg.QueryValueEx(key, field)[0]
                                except OSError:
                                    pass
                    except OSError:
                        continue
                    name = values.get("DriverDesc") or values.get("HardwareInformation.AdapterString")
                    if name:
                        found.append(
                            {"Name": str(name), "AdapterRAM": values.get("HardwareInformation.qwMemorySize")}
                        )
    except OSError:
        return []
    unique = {}
    for item in found:
        unique.setdefault(item["Name"], item)
    return list(unique.values())


def _scan_windows_gpu() -> tuple[str | None, float | None, float | None]:
    """Return the best physical Windows adapter when NVML is unavailable.

    Win32_VideoController.AdapterRAM is useful for many discrete AMD cards,
    but is not dedicated VRAM for Intel/shared adapters. Keep those totals
    unknown rather than feeding a fabricated capacity to fit decisions.
    """
    adapters: dict[str, tuple[str, int | None]] = {}
    # CIM frequently exposes every adapter but AdapterRAM is a legacy
    # 32-bit field. Merge the registry's 64-bit qwMemorySize when available
    # instead of treating CIM order or its truncated value as authoritative.
    rows = _windows_cim("Win32_VideoController", ["Name", "AdapterRAM"])
    rows.extend(_windows_registry_gpus())
    for item in rows:
        name = str(item.get("Name") or "").strip()
        if not name or "microsoft basic" in name.lower() or "remote display" in name.lower():
            continue
        try:
            raw_vram = int(item.get("AdapterRAM"))
            if raw_vram <= 0:
                raw_vram = None
        except (TypeError, ValueError, OverflowError):
            raw_vram = None
        key = " ".join(name.lower().split())
        previous = adapters.get(key)
        if previous is None or (raw_vram or 0) > (previous[1] or 0):
            adapters[key] = (name, raw_vram)
    if not adapters:
        return None, None, None

    def is_integrated(name: str) -> bool:
        lowered = name.lower()
        if "intel" in lowered:
            # Intel Arc A/B-series names denote discrete adapters; generic
            # "Intel Arc Graphics", Iris, and UHD are integrated/shared.
            return re.search(r"\barc\s+[ab]\d", lowered) is None
        return "radeon(tm) graphics" in lowered or "radeon graphics" == lowered.strip()

    # Hybrid laptops often enumerate the integrated adapter first. Prefer a
    # likely discrete adapter, then the largest credible dedicated-memory
    # value, while preserving stable input order for otherwise equal rows.
    ranked = list(adapters.values())
    ranked.sort(
        key=lambda adapter: (
            not is_integrated(adapter[0]),
            adapter[1] is not None,
            adapter[1] or 0,
        ),
        reverse=True,
    )
    name, raw_vram = ranked[0]
    if is_integrated(name):
        return name, None, None
    total = raw_vram / (1024**3) if raw_vram is not None else 0.0
    return name, total if total > 0 else None, None


def _scan_nvidia_vram() -> tuple[str | None, float | None, float | None]:
    """Return (gpu_name, vram_total_gb, vram_free_gb) or (None, None, None) if unavailable."""
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total_gb = mem.total / (1024**3)
            free_gb = mem.free / (1024**3)
            return name, total_gb, free_gb
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None, None, None


def _scan_hardware() -> HardwareInfo:
    try:
        vm = psutil.virtual_memory()
        ram_total_gb = vm.total / (1024**3)
        ram_available_gb = vm.available / (1024**3)
    except OSError:
        # A hardened sandbox/container can deny reads of the memory info
        # psutil relies on (e.g. /proc/meminfo). Fall back to the same 0.0
        # the memory guard already treats as "assume the worst and block"
        # (see calculate_memory_budget) rather than letting this crash
        # verify/install/benchmark's preflight outright.
        ram_total_gb = 0.0
        ram_available_gb = 0.0

    raw_os_name = platform.system()
    os_name = _OS_DISPLAY_NAMES.get(raw_os_name, raw_os_name)
    os_version = platform.release()

    cpu_arch = platform.machine() or "unknown"
    cpu_physical_cores = int(psutil.cpu_count(logical=False) or 0)
    cpu_logical_cores = int(psutil.cpu_count(logical=True) or 0)

    if _is_apple_silicon():
        cpu = _mac_cpu_brand()
        return HardwareInfo(
            os_name=os_name,
            os_version=os_version,
            cpu=cpu,
            ram_total_gb=ram_total_gb,
            ram_available_gb=ram_available_gb,
            unified_memory=True,
            gpu_name=_mac_chip_name(),
            vram_total_gb=ram_total_gb,
            vram_free_gb=ram_available_gb,
            cpu_arch=cpu_arch,
            cpu_physical_cores=cpu_physical_cores,
            cpu_logical_cores=cpu_logical_cores,
        )

    if raw_os_name == "Darwin":
        cpu = _mac_cpu_brand()
    elif raw_os_name == "Linux":
        cpu = _linux_cpu_model() or platform.processor() or cpu_arch
    elif raw_os_name == "Windows":
        cpu = _windows_cpu_model() or platform.processor() or cpu_arch
    else:
        cpu = platform.processor() or cpu_arch

    gpu_name, vram_total_gb, vram_free_gb = _scan_nvidia_vram()
    if gpu_name is None and raw_os_name == "Windows":
        gpu_name, vram_total_gb, vram_free_gb = _scan_windows_gpu()

    return HardwareInfo(
        os_name=os_name,
        os_version=os_version,
        cpu=cpu,
        ram_total_gb=ram_total_gb,
        ram_available_gb=ram_available_gb,
        unified_memory=False,
        gpu_name=gpu_name,
        vram_total_gb=vram_total_gb,
        vram_free_gb=vram_free_gb,
        cpu_arch=cpu_arch,
        cpu_physical_cores=cpu_physical_cores,
        cpu_logical_cores=cpu_logical_cores,
    )


# Snapshot of the most recent scan in this process. A full scan shells out to
# platform tools and is far too slow to run "just in case", so best-effort
# consumers that would merely like hardware context (see omm.error_report)
# reuse whatever the command already scanned instead of forcing one.
_last_scan: HardwareInfo | None = None


def scan_hardware() -> HardwareInfo:
    global _last_scan
    _last_scan = _scan_hardware()
    return _last_scan


def last_scan() -> HardwareInfo | None:
    """The most recent `scan_hardware()` result in this process, or None
    when this command never needed one."""
    return _last_scan
