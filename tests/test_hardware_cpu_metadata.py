from io import StringIO
from types import SimpleNamespace

from omm import hardware


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


def test_windows_discrete_intel_arc_is_not_treated_as_shared(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "_windows_cim",
        lambda *_: [{"Name": "Intel Arc A770", "AdapterRAM": 16 * 1024**3}],
    )
    monkeypatch.setattr(hardware, "_windows_registry_gpus", lambda: [])

    name, total, _ = hardware._scan_windows_gpu()

    assert name == "Intel Arc A770"
    assert total == 16.0
