import json
import threading

from omm import calibration
from omm.hardware import HardwareInfo


def _hardware():
    return HardwareInfo(
        os_name="macOS",
        os_version="26",
        cpu="private raw name",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=True,
        gpu_name="private raw gpu",
        vram_total_gb=16,
        vram_free_gb=12,
    )


def test_record_calibration_stores_coarse_hardware_only(tmp_path):
    path = tmp_path / "calibration.json"

    factor = calibration.record_calibration(
        _hardware(),
        measured_tokens_per_sec=30,
        predicted_tokens_per_sec=20,
        path=path,
    )

    assert factor == 1.5
    assert calibration.calibration_factor(_hardware(), path) == 1.5
    raw = path.read_text()
    assert "private raw" not in raw
    assert json.loads(raw)["profiles"]["macos-ram-16-unified-16"]["sample_count"] == 1


def test_record_calibration_clamps_extreme_ratio(tmp_path):
    path = tmp_path / "calibration.json"
    factor = calibration.record_calibration(
        _hardware(),
        measured_tokens_per_sec=1000,
        predicted_tokens_per_sec=1,
        path=path,
    )
    assert factor == calibration.MAX_FACTOR


def test_record_calibration_concurrent_writers_do_not_lose_updates(tmp_path):
    path = tmp_path / "calibration.json"
    errors = []

    def _record():
        try:
            calibration.record_calibration(
                _hardware(),
                measured_tokens_per_sec=30,
                predicted_tokens_per_sec=20,
                path=path,
            )
        except Exception as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=_record) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    raw = path.read_text()
    payload = json.loads(raw)
    assert payload["profiles"]["macos-ram-16-unified-16"]["sample_count"] == 10
