"""Opt-in (or explicitly forced), best-effort telemetry. Never raises,
never blocks the CLI.

Every attempt (skipped, failed, or sent) is logged locally so a discrepancy
between "how many times I installed" and "how many rows landed on the
server" is diagnosable instead of silently unexplainable. Failed sends are
queued and retried opportunistically on a later `omm` invocation via
`flush_pending()`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from omm import config
from omm.config import load_config

_MAX_LOG_LINES = 500
_DEFAULT_MAX_RETRIES_PER_FLUSH = 3


def secure_endpoint(endpoint: str) -> bool:
    """Allow HTTPS, plus HTTP only for a local self-hosted collector."""
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _log_path():
    return config.OMM_HOME / "telemetry.log"


def _pending_path():
    return config.OMM_HOME / "telemetry_pending.json"


def log_attempt(outcome: str, detail: str = "") -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text().splitlines() if path.exists() else []
        lines.append(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "detail": detail,
        }))
        path.write_text("\n".join(lines[-_MAX_LOG_LINES:]) + "\n")
    except Exception:
        pass


def _load_pending() -> list[dict[str, Any]]:
    path = _pending_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def _save_pending(events: list[dict[str, Any]]) -> None:
    try:
        path = _pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events))
    except OSError:
        pass


def _post_event(event: dict[str, Any]) -> bool:
    """Actually attempt the HTTP POST and log the outcome. Returns True on
    a 2xx response, False otherwise (network error, bad status, or no
    endpoint configured)."""
    import requests

    endpoint = load_config().get("telemetry_endpoint")
    if not isinstance(endpoint, str) or not secure_endpoint(endpoint):
        log_attempt("skipped_no_endpoint")
        return False
    try:
        resp = requests.post(endpoint, json=event, timeout=5)
    except requests.RequestException as e:
        log_attempt("send_failed_network", str(e))
        return False
    if not (200 <= resp.status_code < 300):
        log_attempt(f"send_failed_http_{resp.status_code}")
        return False
    log_attempt("sent_ok")
    return True


def send_event(event: dict[str, Any], force: bool = False) -> bool:
    config_data = load_config()
    if not force and config_data.get("telemetry_send_policy") != "always":
        log_attempt("skipped_opt_out")
        return False
    ok = _post_event(event)
    if not ok:
        events = _load_pending()
        events.append(event)
        _save_pending(events)
    return ok


def flush_pending(max_retries: int = _DEFAULT_MAX_RETRIES_PER_FLUSH) -> int:
    """Best-effort resend of previously-failed events. Retries at most
    `max_retries` events per call so a large backlog can't stall an
    unrelated command. Returns how many were resent successfully."""
    events = _load_pending()
    if not events:
        return 0
    to_retry, still_pending = events[:max_retries], events[max_retries:]
    resent = 0
    for event in to_retry:
        if _post_event(event):
            resent += 1
        else:
            still_pending.append(event)
    _save_pending(still_pending)
    return resent
