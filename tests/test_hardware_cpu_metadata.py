from io import StringIO
from types import SimpleNamespace

import pytest

from omm import hardware


def test_macos_os_version_prefers_product_version_without_kernel_fallback(monkeypatch):
    monkeypatch.setattr(
        hardware.platform,
        "mac_ver",
        lambda: ("26.5.2", ("", "", ""), "arm64"),
    )
    monkeypatch.setattr(
        hardware.platform,
        "release",
        lambda: (_ for _ in ()).throw(AssertionError("kernel fallback must not run")),
    )

    assert hardware._os_version("Darwin") == "26.5.2"


def test_macos_os_version_falls_back_when_product_version_is_empty(monkeypatch):
    monkeypatch.setattr(hardware.platform, "mac_ver", lambda: ("", ("", "", ""), "arm64"))
    monkeypatch.setattr(hardware.platform, "release", lambda: "25.5.0")

    assert hardware._os_version("Darwin") == "25.5.0"


def test_macos_os_version_falls_back_when_product_version_probe_fails(monkeypatch):
    monkeypatch.setattr(
        hardware.platform,
        "mac_ver",
        lambda: (_ for _ in ()).throw(OSError("sw_vers unavailable")),
    )
    monkeypatch.setattr(hardware.platform, "release", lambda: "25.5.0")

    assert hardware._os_version("Darwin") == "25.5.0"


def test_non_macos_os_version_uses_release_without_mac_probe(monkeypatch):
    monkeypatch.setattr(
        hardware.platform,
        "mac_ver",
        lambda: (_ for _ in ()).throw(AssertionError("mac probe must not run")),
    )
    monkeypatch.setattr(hardware.platform, "release", lambda: "6.8.0")

    assert hardware._os_version("Linux") == "6.8.0"


def test_macos_cpu_probe_is_bounded_and_fails_soft_on_timeout(monkeypatch):
    seen = {}

    def timeout(*args, **kwargs):
        seen.update(kwargs)
        raise hardware.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(hardware.subprocess, "run", timeout)

    assert hardware._mac_sysctl_cpu_brand() is None
    assert seen["timeout"] == 3


def test_commit_counters_report_both_headroom_and_the_whole_limit():
    info = hardware._windows_commit_info(3_000_000, 5_000_000, 4096)
    assert info.available_gb == 7.62939453125
    assert info.limit_gb == 19.073486328125
    # CommitTotal above CommitLimit is incoherent; refuse to guess.
    assert hardware._windows_commit_info(5, 4, 4096) is None


def test_available_commit_returns_none_off_windows_without_touching_win32(monkeypatch):
    """The Win32-only import must stay behind the platform check.

    Callers treat None as "no commit signal, use the portable physical
    fallback", so a non-Windows host has to reach that branch before any
    ctypes.wintypes work happens.
    """
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    assert hardware.windows_commit_info() is None


def test_linux_scan_uses_cpu_model_and_core_counts_not_architecture(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "release", lambda: "6.8")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "")
    monkeypatch.setattr(
        hardware.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=32 * 1024**3, available=24 * 1024**3),
    )
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical: 12 if logical else 6)
    monkeypatch.setattr(hardware, "_scan_nvidia_vram", lambda: (None, None, None))
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: StringIO("model name\t: AMD Ryzen 5 5600X 6-Core Processor\n"),
    )

    info = hardware.scan_hardware()

    assert info.cpu == "AMD Ryzen 5 5600X 6-Core Processor"
    assert info.cpu_arch == "x86_64"
    assert info.cpu_physical_cores == 6
    assert info.cpu_logical_cores == 12


def test_scan_survives_psutil_permission_denied(monkeypatch):
    """A hardened sandbox/container can deny reads of the memory info
    psutil relies on (e.g. /proc/meminfo). This used to raise straight out
    of scan_hardware() and crash verify/install/benchmark's preflight -
    it must instead fall back to the same 0.0 the memory guard already
    treats as "assume the worst and block"."""
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "release", lambda: "6.8")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "")

    def _denied():
        raise PermissionError("Permission denied: '/proc/meminfo'")

    monkeypatch.setattr(hardware.psutil, "virtual_memory", _denied)
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical: 12 if logical else 6)
    monkeypatch.setattr(hardware, "_scan_nvidia_vram", lambda: (None, None, None))
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: StringIO("model name\t: AMD Ryzen 5 5600X 6-Core Processor\n"),
    )

    info = hardware.scan_hardware()

    assert info.ram_total_gb == 0.0
    assert info.ram_available_gb == 0.0


def test_scan_survives_psutil_specific_errors(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "release", lambda: "6.8")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "CPU")
    monkeypatch.setattr(
        hardware.psutil,
        "virtual_memory",
        lambda: (_ for _ in ()).throw(hardware.psutil.AccessDenied()),
    )
    monkeypatch.setattr(
        hardware.psutil,
        "cpu_count",
        lambda logical: (_ for _ in ()).throw(hardware.psutil.AccessDenied()),
    )
    monkeypatch.setattr(hardware, "_scan_nvidia_vram", lambda: (None, None, None))
    monkeypatch.setattr(hardware, "_linux_cpu_model", lambda: "CPU")

    info = hardware.scan_hardware()

    assert info.ram_total_gb == 0.0
    assert info.cpu_physical_cores == 0
    assert info.cpu_logical_cores == 0


def test_available_ram_fails_closed_on_psutil_error(monkeypatch):
    monkeypatch.setattr(
        hardware.psutil,
        "virtual_memory",
        lambda: (_ for _ in ()).throw(hardware.psutil.AccessDenied()),
    )

    assert hardware.available_ram_gb() == 0.0


def test_windows_scan_uses_cim_cpu_and_integrated_gpu_name(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hardware.platform, "release", lambda: "11")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "Intel64 Family 6")
    monkeypatch.setattr(
        hardware.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16 * 1024**3, available=8 * 1024**3),
    )
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical: 22 if logical else 16)
    monkeypatch.setattr(hardware, "_scan_nvidia_vram", lambda: (None, None, None))

    def fake_cim(class_name, properties):
        if class_name == "Win32_Processor":
            return [{"Name": " Intel(R) Core(TM) Ultra 7 155H "}]
        return [{"Name": "Intel(R) Arc(TM) Graphics", "AdapterRAM": 2 * 1024**3}]

    monkeypatch.setattr(hardware, "_windows_cim", fake_cim)
    monkeypatch.setattr(hardware, "_windows_registry_gpus", lambda: [])

    info = hardware.scan_hardware()

    assert info.cpu == "Intel(R) Core(TM) Ultra 7 155H"
    assert info.gpu_name == "Intel(R) Arc(TM) Graphics"
    assert info.vram_total_gb is None  # shared memory must not become fake dedicated VRAM


def test_windows_hybrid_gpu_prefers_discrete_and_registry_vram(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "_windows_cim",
        lambda *_: [
            {"Name": "Intel(R) Arc(TM) Graphics", "AdapterRAM": 2 * 1024**3},
            {"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 4 * 1024**3},
        ],
    )
    monkeypatch.setattr(
        hardware,
        "_windows_registry_gpus",
        lambda: [{"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 16 * 1024**3}],
    )

    name, total, free = hardware._scan_windows_gpu()

    assert name == "AMD Radeon RX 7800 XT"
    assert total == 16.0
    assert free is None


@pytest.mark.parametrize(
    ("name", "ram_gb", "expected"),
    [
        ("Intel(R) Arc(TM) A770 Graphics", 16, 16.0),
        ("Intel(R) Arc(TM) B580 Graphics", 12, 12.0),
        ("Intel(R) Arc(TM) Pro A60 Graphics", 12, 12.0),
        ("Intel Arc A770", 16, 16.0),
        ("Intel(R) Arc(TM) Graphics", 2, None),
        ("Intel(R) Arc(TM) 140V GPU (16GB)", 2, None),
    ],
)
def test_windows_discrete_intel_arc_is_not_treated_as_shared(monkeypatch, name, ram_gb, expected):
    monkeypatch.setattr(
        hardware,
        "_windows_cim",
        lambda *_: [{"Name": name, "AdapterRAM": ram_gb * 1024**3}],
    )
    monkeypatch.setattr(hardware, "_windows_registry_gpus", lambda: [])

    got_name, total, _ = hardware._scan_windows_gpu()

    assert got_name == name
    assert total == expected


@pytest.mark.parametrize(
    ("name", "ram_gb", "expected"),
    [
        ("AMD Radeon(TM) Graphics", 2, None),
        ("AMD Radeon(TM) Vega 8 Graphics", 2, None),
        ("AMD Radeon(TM) 780M Graphics", 2, None),
        ("AMD Radeon 890M Graphics", 4, None),
        ("AMD Radeon RX 7800 XT", 16, 16.0),
        ("AMD Radeon RX 6600M", 8, 8.0),
        ("AMD Radeon Pro W5700", 8, 8.0),
        ("AMD Radeon HD 7970", 3, 3.0),
    ],
)
def test_windows_amd_apu_names_are_not_reported_as_dedicated_vram(monkeypatch, name, ram_gb, expected):
    monkeypatch.setattr(hardware, "_windows_cim", lambda *_: [{"Name": name, "AdapterRAM": ram_gb * 1024**3}])
    monkeypatch.setattr(hardware, "_windows_registry_gpus", lambda: [])
    got_name, total, _ = hardware._scan_windows_gpu()
    assert got_name == name
    assert total == expected


def test_registry_gpu_dedupe_prefers_the_row_that_has_qwmemorysize(monkeypatch):
    """Driver reinstalls can leave a value-less registry row enumerated before
    the one that actually has qwMemorySize; dedupe must keep the larger size
    instead of first-wins, matching how _scan_windows_gpu merges duplicates.
    """
    import sys

    class _FakeKey:
        def __init__(self, path, values=None, subkeys=None):
            self.path = path
            self.values = values or {}
            self.subkeys = subkeys or []

    hklm_sentinel = object()
    base_path = r"SYSTEM\CurrentControlSet\Control\Video"

    root_guid_key = _FakeKey(base_path, subkeys=["GUID0"])
    guid_key = _FakeKey(f"{base_path}\\GUID0", subkeys=["0000", "0001"])
    key_0000 = _FakeKey(
        f"{base_path}\\GUID0\\0000", values={"DriverDesc": "NVIDIA GeForce RTX 4090"}
    )
    key_0001 = _FakeKey(
        f"{base_path}\\GUID0\\0001",
        values={"DriverDesc": "NVIDIA GeForce RTX 4090", "HardwareInformation.qwMemorySize": 24 * 1024**3},
    )

    tree = {
        base_path: root_guid_key,
        f"{base_path}\\GUID0": guid_key,
        f"{base_path}\\GUID0\\0000": key_0000,
        f"{base_path}\\GUID0\\0001": key_0001,
    }

    class _OpenKeyCtx:
        def __init__(self, fake_key):
            self.fake_key = fake_key

        def __enter__(self):
            return self.fake_key

        def __exit__(self, *exc_info):
            return False

    class _FakeWinreg:
        HKEY_LOCAL_MACHINE = hklm_sentinel

        @staticmethod
        def OpenKey(root, path):
            full_path = path if root is hklm_sentinel else f"{root.path}\\{path}"
            fake_key = tree[full_path]
            return _OpenKeyCtx(fake_key)

        @staticmethod
        def QueryInfoKey(fake_key):
            return (len(fake_key.subkeys), 0, 0)

        @staticmethod
        def EnumKey(fake_key, index):
            return fake_key.subkeys[index]

        @staticmethod
        def QueryValueEx(fake_key, field):
            if field not in fake_key.values:
                raise OSError("not found")
            return (fake_key.values[field], None)

    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg)

    result = hardware._windows_registry_gpus()

    assert result == [{"Name": "NVIDIA GeForce RTX 4090", "AdapterRAM": 24 * 1024**3}]
