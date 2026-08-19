"""Private, machine-local correction for recommendation speed estimates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from omm.config import CALIBRATION_PATH
from omm.hardware import HardwareInfo

MIN_FACTOR = 0.25
MAX_FACTOR = 4.0


def hardware_bucket(hardware: HardwareInfo) -> str:
    """Build a coarse key without saving raw CPU/GPU names."""
    ram = max(1, round(hardware.ram_total_gb / 4) * 4)
    if hardware.unified_memory:
        accelerator = f"unified-{ram}"
    elif hardware.vram_total_gb:
        vram = max(1, round(hardware.vram_total_gb / 2) * 2)
        accelerator = f"vram-{vram}"
    else:
        accelerator = "cpu"
    return f"{hardware.os_name.lower()}-ram-{ram}-{accelerator}"


def load_profiles(path: Path | None = None) -> dict:
    target = path or CALIBRATION_PATH
    if not target.exists():
        return {"schema_version": 1, "profiles": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "profiles": {}}
    if payload.get("schema_version") != 1 or not isinstance(payload.get("profiles"), dict):
        return {"schema_version": 1, "profiles": {}}
    return payload


def _profile_key(hardware: HardwareInfo, engine: str) -> str:
    bucket = hardware_bucket(hardware)
    return bucket if engine == "ollama" else f"{bucket}|{engine}"


def calibration_factor(
    hardware: HardwareInfo,
    path: Path | None = None,
    *,
    engine: str = "ollama",
) -> float:
    profile = load_profiles(path)["profiles"].get(_profile_key(hardware, engine), {})
    factor = profile.get("factor", 1.0)
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        return 1.0
    return max(MIN_FACTOR, min(MAX_FACTOR, float(factor)))


def record_calibration(
    hardware: HardwareInfo,
    *,
    measured_tokens_per_sec: float,
    predicted_tokens_per_sec: float,
    path: Path | None = None,
    engine: str = "ollama",
) -> float:
    if measured_tokens_per_sec <= 0 or predicted_tokens_per_sec <= 0:
        raise ValueError("Calibration speeds must be greater than zero.")
    target = path or CALIBRATION_PATH
    payload = load_profiles(target)
    key = _profile_key(hardware, engine)
    previous = payload["profiles"].get(key, {})
    new_ratio = max(
        MIN_FACTOR,
        min(MAX_FACTOR, measured_tokens_per_sec / predicted_tokens_per_sec),
    )
    previous_factor = previous.get("factor")
    previous_samples = previous.get("sample_count", 0)
    if isinstance(previous_factor, (int, float)) and isinstance(previous_samples, int):
        sample_count = min(previous_samples, 9)
        factor = (float(previous_factor) * sample_count + new_ratio) / (sample_count + 1)
        total_samples = previous_samples + 1
    else:
        factor = new_ratio
        total_samples = 1
    payload["profiles"][key] = {
        "factor": round(max(MIN_FACTOR, min(MAX_FACTOR, factor)), 6),
        "sample_count": total_samples,
        "last_measured_tokens_per_sec": round(measured_tokens_per_sec, 4),
        "last_predicted_tokens_per_sec": round(predicted_tokens_per_sec, 4),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return payload["profiles"][key]["factor"]
