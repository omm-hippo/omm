"""Build a local, allow-listed support report without any send path."""

from __future__ import annotations

import json
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from omm import config, package_metadata

SCHEMA_VERSION = 1
OPTIONAL_GROUPS = ("os", "network", "policies", "checks")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_. -]{1,80}")


def _version() -> str:
    try:
        return package_metadata.version()
    except Exception:
        return "unknown"


def _install_source() -> str:
    try:
        return package_metadata.install_source().value
    except Exception:
        return "unknown"


def latest_command_name() -> str | None:
    """Read only the already-scrubbed command token from the latest run log."""
    logs = config.OMM_HOME / "logs"
    try:
        paths = sorted(logs.glob("*.jsonl"), reverse=True)
    except OSError:
        return None
    for path in paths[:50]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines[:3]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            argv = record.get("argv") if isinstance(record, dict) else None
            if not isinstance(argv, list):
                continue
            for token in argv:
                if (
                    isinstance(token, str)
                    and not token.startswith("-")
                    and token not in {"<arg>", "report"}
                    and _SAFE_NAME.fullmatch(token)
                ):
                    return token
    return None


def latest_error_type() -> str | None:
    path = config.OMM_HOME / "error_reports_pending.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        value = row.get("error_type") if isinstance(row, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value):
            return value
    return None


def build(doctor_report, *, include: Iterable[str] = ()) -> dict[str, object]:
    selected = set(include)
    unknown = selected - set(OPTIONAL_GROUPS)
    if unknown:
        raise ValueError(f"unknown report field group: {', '.join(sorted(unknown))}")
    counts = {
        status: sum(check.status == status for check in doctor_report.checks)
        for status in ("PASS", "WARN", "FAIL")
    }
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "omm_version": _version(),
        "install_source": _install_source(),
        "command_name": latest_command_name(),
        "error_type": latest_error_type(),
        "diagnostics": {"status": doctor_report.status, "counts": counts},
    }
    if "os" in selected:
        report["os"] = {"name": platform.system() or "unknown", "arch": platform.machine() or "unknown"}
    if "network" in selected:
        from omm import network_policy

        report["network"] = {"mode": network_policy.current_mode()}
    if "policies" in selected:
        try:
            data = config.load_config()
        except Exception:
            data = {}
        report["upload_policies"] = {
            "benchmark": data.get("telemetry_send_policy", "ask"),
            "usage": "enabled" if data.get("usage_stats_policy") == "enabled" else "off",
            "crash": data.get("error_report_send_policy") or "off",
        }
    if "checks" in selected:
        report["checks"] = [
            {"name": check.name[:80], "status": check.status}
            for check in doctor_report.checks
            if _SAFE_NAME.fullmatch(check.name[:80])
        ]
    return report


def preview(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
