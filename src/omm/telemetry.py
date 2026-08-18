"""Opt-in (or explicitly forced), best-effort telemetry. Never raises.

Every attempt (skipped, failed, or sent) is logged locally so a discrepancy
between "how many times I installed" and "how many rows landed on the
server" is diagnosable instead of silently unexplainable. Failed sends made
under the persistent ``always`` policy are queued and retried opportunistically
on a later `omm` invocation via `flush_pending()`. One-shot consent is never
converted into an unattended future send.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.atomic import atomic_write_text, locked
from omm.config import load_config

_MAX_LOG_LINES = 500
_MAX_PENDING_EVENTS = 1000
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
        with locked(path, timeout=30):
            lines = path.read_text().splitlines() if path.exists() else []
            lines.append(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,
                "detail": detail,
            }))
            atomic_write_text(path, "\n".join(lines[-_MAX_LOG_LINES:]) + "\n")
    except (OSError, FileLockTimeout):
        pass


def _read_pending_unlocked(path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [event for event in loaded if isinstance(event, dict)][-_MAX_PENDING_EVENTS:]


def _load_pending() -> list[dict[str, Any]]:
    path = _pending_path()
    try:
        with locked(path, timeout=30):
            return _read_pending_unlocked(path)
    except (OSError, FileLockTimeout):
        return []


def _save_pending(events: list[dict[str, Any]]) -> None:
    try:
        path = _pending_path()
        with locked(path, timeout=30):
            atomic_write_text(path, json.dumps(events[-_MAX_PENDING_EVENTS:]))
    except (OSError, FileLockTimeout):
        pass


def _append_pending(event: dict[str, Any]) -> None:
    """Append without losing events written by another omm process."""
    try:
        path = _pending_path()
        with locked(path, timeout=30):
            events = _read_pending_unlocked(path)
            events.append(event)
            atomic_write_text(path, json.dumps(events[-_MAX_PENDING_EVENTS:]))
    except (OSError, FileLockTimeout):
        pass


def _post_event(event: dict[str, Any]) -> bool:
    """Actually attempt the HTTP POST and log the outcome. Returns True on
    a 2xx response, False otherwise (network error, bad status, no endpoint
    configured, or - for the hosted Firebase collector, whose RTDB rules
    require `auth != null` - no anonymous auth token available)."""
    import requests

    endpoint = load_config().get("telemetry_endpoint")
    if not isinstance(endpoint, str) or not secure_endpoint(endpoint):
        log_attempt("skipped_no_endpoint")
        return False

    params = {}
    if "firebaseio.com" in endpoint:
        from omm import firebase_auth

        id_token = firebase_auth.get_id_token()
        if id_token is None:
            log_attempt("send_failed_no_auth_token")
            return False
        params["auth"] = id_token

    try:
        resp = requests.post(endpoint, params=params, json=event, timeout=5)
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
    # Only persistent opt-in authorizes an unattended retry in a later
    # process. A one-shot --upload/confirmation authorizes this attempt, not
    # an indefinite background queue after the user may have opted out.
    if not ok and config_data.get("telemetry_send_policy") == "always":
        _append_pending(event)
    return ok


def flush_pending(max_retries: int = _DEFAULT_MAX_RETRIES_PER_FLUSH) -> int:
    """Best-effort resend of previously-failed events. Retries at most
    `max_retries` events per call so a large backlog can't stall an
    unrelated command. Returns how many were resent successfully."""
    # Re-check consent at send time. This prevents an old queue from bypassing
    # a later `setting upload --disable`, including commands whose root
    # callback runs before the setting subcommand body.
    if load_config().get("telemetry_send_policy") != "always":
        return 0
    path = _pending_path()
    try:
        # Serialize the whole bounded retry batch so a concurrent writer
        # cannot be erased by this read/modify/write cycle.
        with locked(path, timeout=30):
            events = _read_pending_unlocked(path)
            if not events:
                return 0
            to_retry, still_pending = events[:max_retries], events[max_retries:]
            resent = 0
            for event in to_retry:
                if _post_event(event):
                    resent += 1
                else:
                    still_pending.append(event)
            atomic_write_text(path, json.dumps(still_pending[-_MAX_PENDING_EVENTS:]))
            return resent
    except (OSError, FileLockTimeout):
        return 0
