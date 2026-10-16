import pytest

from omm import memory_group as mg
from omm.hardware import HardwareInfo


def _hw(*, unified=False, ram=16.0, vram=None):
    return HardwareInfo(
        os_name="x",
        os_version="x",
        cpu="x",
        ram_total_gb=ram,
        ram_available_gb=ram,
        unified_memory=unified,
        gpu_name=None,
        vram_total_gb=vram,
        vram_free_gb=None,
    )


@pytest.mark.parametrize(
    "gib,label",
    [
        (8, "8"), (7.9, "8"), (16, "16"), (15.8, "16"), (18, "18-24"), (24, "18-24"),
        (32, "32-36"), (36, "32-36"), (48, "48-64"), (64, "48-64"), (96, "96-128"),
        (128, "96-128"), (192, "192+"),
    ],
)
def test_unified_marketed_sizes(gib, label):
    assert mg.bucket_for("unified", gib) == label


@pytest.mark.parametrize(
    "gib,label",
    [
        (4, "0-6"), (6, "0-6"), (7.98, "8"), (8, "8"), (10, "10-12"), (12, "10-12"),
        (16, "16"), (15.99, "16"), (20, "20-24"), (23.99, "20-24"), (24, "20-24"),
        (32, "32+"), (48, "32+"),
    ],
)
def test_vram_marketed_sizes(gib, label):
    assert mg.bucket_for("vram", gib) == label


@pytest.mark.parametrize(
    "gib,label", [(8, "0-8"), (16, "16"), (32, "32"), (31.6, "32"), (64, "64+")]
)
def test_cpu_marketed_sizes(gib, label):
    assert mg.bucket_for("cpu", gib) == label


def test_every_bucket_for_result_is_a_declared_label():
    for kind, labels in mg.BUCKET_LABELS.items():
        for gib in range(1, 300):
            assert mg.bucket_for(kind, gib) in labels


def test_apple_silicon_groups_by_ram():
    hw = _hw(unified=True, ram=15.8, vram=15.8)
    assert mg.classify(hw, system="Darwin") == ("unified", "16")


def test_pc_with_gpu_groups_by_vram_on_linux_and_windows():
    assert mg.classify(_hw(ram=64, vram=23.99), system="Linux") == ("vram", "20-24")
    assert mg.classify(_hw(ram=64, vram=7.98), system="Windows") == ("vram", "8")


def test_no_gpu_is_cpu_only_where_detection_is_trustworthy():
    assert mg.classify(_hw(ram=31.6), system="Windows") == ("cpu", "32")
    # An Intel Mac: no unified memory, no NVML GPU.
    assert mg.classify(_hw(ram=16), system="Darwin") == ("cpu", "16")


def test_linux_without_detected_gpu_sends_no_group():
    """hardware.py reads VRAM through NVML only on Linux, so an AMD machine
    Ollama runs on the GPU would be mislabelled "CPU only"."""
    assert mg.classify(_hw(ram=32), system="Linux") is None


@pytest.mark.parametrize(
    "hw,system,expected",
    [
        (_hw(unified=True, ram=0.0, vram=0.0), "Darwin", None),  # sandboxed psutil
        (_hw(ram=0.0), "Windows", None),
        (_hw(ram=float("nan")), "Windows", None),
        (_hw(ram=32, vram=0.0), "Windows", ("cpu", "32")),  # 0 VRAM = no usable GPU
        (_hw(ram=32), "FreeBSD", None),
    ],
)
def test_unusable_readings_send_no_group(hw, system, expected):
    assert mg.classify(hw, system=system) == expected


def test_is_valid_rejects_cross_kind_buckets():
    assert mg.is_valid("vram", "8")
    assert not mg.is_valid("unified", "0-6")
    assert not mg.is_valid("gpu", "8")
    assert not mg.is_valid("cpu", None)


@pytest.mark.parametrize(
    "kind,bucket,label",
    [
        ("unified", "16", "Mac · unified memory 16GB"),
        ("unified", "18-24", "Mac · unified memory 18–24GB"),
        ("vram", "20-24", "PC · GPU VRAM 20–24GB"),
        ("vram", "0-6", "PC · GPU VRAM ≤6GB"),
        ("cpu", "64+", "CPU only · RAM 64GB+"),
        ("vram", "7", "vram:7"),
    ],
)
def test_display_label(kind, bucket, label):
    assert mg.display_label(kind, bucket) == label
