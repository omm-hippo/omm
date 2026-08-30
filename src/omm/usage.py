"""Strictly opt-in, best-effort anonymous usage statistics. Never raises.

One coarse snapshot per day (install identity, version, bucketed hardware)
plus a tally of which commands ran and whether they succeeded. Built from
purpose-made records only - never a dump of the local run log. Sent through
the same proof-of-work gateway as telemetry, to a separate write-only RTDB
node (``/usage``). Off unless ``usage_stats_policy == "enabled"``.

Mirrors :mod:`omm.error_report` on purpose: same pending-queue /
``flush_pending`` / attempt-log shape, so there is one pattern to reason
about. See ``docs/superpowers/specs/2026-08-30-usage-stats-design.md`` and
``PRIVACY.md``.
"""

from __future__ import annotations

import json
import platform
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.atomic import atomic_write_text, locked

SCHEMA_VERSION = 1
_PENDING_MAX = 5000
_TALLY_MAX_KEYS = 100
_FLUSH_INTERVAL_S = 24 * 3600
_MAX_LOG_LINES = 500
_DETAIL_SLICE = 300


def _pending_path():
    return config.OMM_HOME / "usage-pending.json"


def _state_path():
    return config.OMM_HOME / "usage-state.json"


def _backoff_path():
    return config.OMM_HOME / "usage-backoff.json"


def _log_path():
    return config.OMM_HOME / "usage.log"


def policy(config_data: dict[str, Any] | None = None) -> str:
    data = config_data if config_data is not None else config.load_config()
    return "enabled" if data.get("usage_stats_policy") == "enabled" else "never"


# --- collection --------------------------------------------------------


def _read_pending() -> list[dict]:
    try:
        with locked(_pending_path(), timeout=10):
            path = _pending_path()
            if not path.exists():
                return []
            loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            return []
        return [r for r in loaded if isinstance(r, dict)][-_PENDING_MAX:]
    except (OSError, ValueError, FileLockTimeout):
        return []


def record_run(subcommand: str, outcome: str, error_class: str | None) -> None:
    """Append one row for this invocation. No-op unless opted in. No
    network. Swallows all errors."""
    try:
        if policy() != "enabled":
            return
        row = {"c": str(subcommand)[:40], "o": str(outcome)[:20]}
        if error_class and outcome == "failed":
            row["e"] = str(error_class)[:60]
        with locked(_pending_path(), timeout=10):
            path = _pending_path()
            rows: list[dict] = []
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        rows = [r for r in loaded if isinstance(r, dict)]
                except ValueError:
                    rows = []
            rows.append(row)
            atomic_write_text(path, json.dumps(rows[-_PENDING_MAX:]))
    except (OSError, FileLockTimeout):
        pass


def pending_count() -> int:
    return len(_read_pending())


def discard_pending() -> int:
    try:
        n = pending_count()
        with locked(_pending_path(), timeout=10):
            _pending_path().unlink(missing_ok=True)
        return n
    except (OSError, FileLockTimeout):
        return 0


# --- snapshot + aggregation ------------------------------------------

_RAM_BUCKETS = [(8, "<8"), (16, "8-16"), (32, "16-32"), (64, "32-64"), (128, "64-128")]
_VRAM_BUCKETS = [(4, "<4"), (8, "4-8"), (12, "8-12"), (16, "12-16"), (24, "16-24")]


def _bucket(value: float | None, table, top: str) -> str:
    if value is None:
        return "none"
    for edge, label in table:
        if value < edge:
            return label
    return top


def _gpu_vendor(gpu_name: str | None) -> str:
    if not gpu_name:
        return "none"
    low = gpu_name.lower()
    if "apple" in low or "m1" in low or "m2" in low or "m3" in low or "m4" in low:
        return "apple"
    if any(m in low for m in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")):
        return "nvidia"
    if any(m in low for m in ("amd", "radeon", "rx ")):
        return "amd"
    if any(m in low for m in ("intel", "arc", "iris", "uhd")):
        return "intel"
    return "other"


def _snapshot() -> dict:
    from omm import package_metadata

    try:
        source = package_metadata.install_source().value
    except Exception:
        source = "unknown"
    try:
        client_version = package_metadata.version() or "unknown"
    except Exception:
        client_version = "unknown"
    try:
        from omm import hardware

        hw = hardware.scan_hardware()
        ram, vram, gpu = hw.ram_total_gb, hw.vram_total_gb, hw.gpu_name
        arch = hw.cpu_arch or platform.machine()
    except Exception:
        ram = vram = gpu = None
        arch = platform.machine()
    try:
        channel = "beta" if config.load_config().get("update_channel") == "beta" else "stable"
    except Exception:
        channel = "stable"
    return {
        "schema_version": SCHEMA_VERSION,
        "client_id": config.client_id(),
        "client_version": client_version,
        "install_source": source,
        "os_name": platform.system() or "unknown",
        "os_version": (platform.release() or "")[:64],
        "cpu_arch": (arch or "unknown")[:64],
        "ram_gb_bucket": _bucket(ram, _RAM_BUCKETS, "128+"),
        "vram_gb_bucket": _bucket(vram, _VRAM_BUCKETS, "24+"),
        "gpu_vendor": _gpu_vendor(gpu),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "update_channel": channel,
    }


def _aggregate(rows: list[dict]) -> tuple[dict, dict]:
    commands: Counter = Counter()
    errors: Counter = Counter()
    for r in rows:
        cmd = r.get("c", "unknown")
        out = r.get("o", "unknown")
        commands[f"{cmd} {out}"] += 1
        if r.get("e"):
            errors[f"{cmd} {r['e']}"] += 1
    return (
        dict(sorted(commands.items())[:_TALLY_MAX_KEYS]),
        dict(sorted(errors.items())[:_TALLY_MAX_KEYS]),
    )


def build_payload() -> dict:
    """Snapshot + aggregated tally of pending rows. Used by the sender and
    by ``omm setting upload usage``'s dry-run preview."""
    payload = _snapshot()
    commands, errors = _aggregate(_read_pending())
    payload["commands"] = commands
    if errors:
        payload["errors"] = errors
    return payload


# --- flush ------------------------------------------------------------


def log_attempt(outcome: str, detail: str = "") -> None:
    try:
        with locked(_log_path(), timeout=10):
            path = _log_path()
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,
                "detail": detail[:_DETAIL_SLICE],
            }))
            atomic_write_text(path, "\n".join(lines[-_MAX_LOG_LINES:]) + "\n")
    except (OSError, FileLockTimeout):
        pass


def _read_json(path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_state() -> dict:
    return _read_json(_state_path())


def _stamp_state() -> None:
    try:
        with locked(_state_path(), timeout=10):
            atomic_write_text(_state_path(), json.dumps({"last_sent": time.time()}))
    except (OSError, FileLockTimeout):
        pass


def _backoff_active() -> bool:
    try:
        return time.time() < float(_read_json(_backoff_path()).get("until", 0))
    except (TypeError, ValueError):
        return False


def _set_backoff(seconds: float) -> None:
    try:
        with locked(_backoff_path(), timeout=10):
            atomic_write_text(
                _backoff_path(), json.dumps({"until": time.time() + seconds})
            )
    except (OSError, FileLockTimeout):
        pass


def _clear_backoff() -> None:
    try:
        with locked(_backoff_path(), timeout=10):
            _backoff_path().unlink(missing_ok=True)
    except (OSError, FileLockTimeout):
        pass


def _post_to(endpoint: str, payload: dict) -> bool:
    """POST one PoW-signed batch. Only ``config.USAGE_GATEWAY_ENDPOINT`` is
    ever an allowed target - this stream has no self-hosted variant."""
    if endpoint != config.USAGE_GATEWAY_ENDPOINT:
        log_attempt("skipped_bad_endpoint")
        return False
    import requests

    from omm.telemetry import _solve_proof_of_work

    wire = {k: v for k, v in payload.items() if v is not None}
    event_json = json.dumps(wire, sort_keys=True, separators=(",", ":"))
    timestamp_ms, nonce = _solve_proof_of_work(event_json)
    try:
        resp = requests.post(
            endpoint,
            json={"event_json": event_json, "timestamp": timestamp_ms, "nonce": nonce},
            timeout=10,
        )
    except requests.RequestException as e:
        log_attempt("send_failed_network", str(e))
        return False
    if 200 <= resp.status_code < 300:
        log_attempt("sent_ok")
        return True
    log_attempt(
        f"send_failed_http_{resp.status_code}",
        str(getattr(resp, "text", "") or ""),
    )
    return False


def _post(payload: dict) -> bool:
    return _post_to(config.USAGE_GATEWAY_ENDPOINT, payload)


def flush_pending(force: bool = False) -> bool:
    """Send one batch if opted in, past the 24h interval, and not backing
    off. Clears pending + stamps state on success. One POST per call.
    Swallows all errors; returns whether it sent."""
    try:
        if policy() != "enabled":
            return False
        rows = _read_pending()
        if not rows and not force:
            return False
        if not force:
            if _backoff_active():
                return False
            last = float(_read_state().get("last_sent", 0) or 0)
            if time.time() - last < _FLUSH_INTERVAL_S:
                return False
        if _post(build_payload()):
            discard_pending()
            _stamp_state()
            _clear_backoff()
            return True
        _set_backoff(6 * 3600)
        return False
    except Exception:
        return False
