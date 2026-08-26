import json
import math
import threading

import pytest

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


def test_invalid_profile_shapes_and_nonfinite_values_fall_back_safely(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text("[]")
    assert calibration.calibration_factor(_hardware(), path) == 1.0

    key = calibration.hardware_bucket(_hardware())
    path.write_text(
        json.dumps({"schema_version": 1, "profiles": {key: {"factor": math.nan}}})
    )
    assert calibration.calibration_factor(_hardware(), path) == 1.0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_record_calibration_rejects_nonfinite_or_boolean_speeds(tmp_path, value):
    with pytest.raises(ValueError, match="greater than zero"):
        calibration.record_calibration(
            _hardware(),
            measured_tokens_per_sec=value,
            predicted_tokens_per_sec=1,
            path=tmp_path / "calibration.json",
        )


def test_corrupt_negative_sample_count_does_not_divide_by_zero(tmp_path):
    path = tmp_path / "calibration.json"
    key = calibration.hardware_bucket(_hardware())
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {key: {"factor": 2.0, "sample_count": -1}},
            }
        )
    )

    assert calibration.record_calibration(
        _hardware(),
        measured_tokens_per_sec=30,
        predicted_tokens_per_sec=20,
        path=path,
    ) == 1.5


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
