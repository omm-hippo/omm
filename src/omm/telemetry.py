"""Opt-in (or explicitly forced), best-effort telemetry. Never raises.

Every attempt (skipped, failed, or sent) is logged locally so a discrepancy
between "how many times I installed" and "how many rows landed on the
server" is diagnosable instead of silently unexplainable. Failed sends made
under the persistent ``always`` policy are queued and retried opportunistically
on a later `omm` invocation via `flush_pending()`. One-shot consent is never
converted into an unattended future send.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
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
_CONTRIBUTE_SEND_ATTEMPTS = 3
_MAX_FAILURE_DETAIL_LENGTH = 300
# Must match the Cloudflare Worker's POW_DIFFICULTY_PREFIX_LENGTH
# (cf-worker/wrangler.toml) - a mismatch makes every submission fail, since
# the server only accepts nonces meeting *its* target. See omm-hippo/omm#133.
_POW_DIFFICULTY_PREFIX_LENGTH = 5
_POW_DIFFICULTY_PREFIX = "0" * _POW_DIFFICULTY_PREFIX_LENGTH


@dataclass(frozen=True)
class SendStatus:
    """Machine-readable outcome for the most recent HTTP send attempt."""

    outcome: str
    status_code: int | None = None
    detail: str = ""
    retryable: bool = False
    attempts: int = 1


_last_send_status: SendStatus | None = None


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


def last_failed_path():
    return config.OMM_HOME / "telemetry_last_failed.json"


def last_send_status() -> SendStatus | None:
    return _last_send_status


def _set_send_status(status: SendStatus | None) -> None:
    global _last_send_status
    _last_send_status = status


def reset_send_status() -> None:
    _set_send_status(None)


def _save_last_failed(event: dict[str, Any], status: SendStatus) -> None:
    """Keep one local diagnostic copy without authorizing a future send.

    The event contains only the telemetry payload the user just consented to
    send, never generated model text. It is overwritten on the next failure
    and is deliberately separate from the unattended retry queue.
    """
    try:
        path = last_failed_path()
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "failure": {
                "outcome": status.outcome,
                "status_code": status.status_code,
                "detail": status.detail,
                "attempts": status.attempts,
            },
            "event": event,
        }
        with locked(path, timeout=30):
            atomic_write_text(path, json.dumps(payload, sort_keys=True))
    except (OSError, FileLockTimeout, TypeError, ValueError):
        pass


def log_attempt(outcome: str, detail: str = "") -> None:
    try:
        path = _log_path()
        with locked(path, timeout=30):
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
        loaded = json.loads(path.read_text(encoding="utf-8"))
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


def _solve_proof_of_work(event_json: str) -> tuple[int, int]:
    """Find (timestamp_ms, nonce) such that
    SHA256(f"{event_json}:{timestamp_ms}:{nonce}") starts with
    _POW_DIFFICULTY_PREFIX. Binding the puzzle to the exact payload string
    and a fresh timestamp is what the gateway checks - see cf-worker/src/pow.ts
    and omm-hippo/omm#133."""
    timestamp_ms = int(time.time() * 1000)
    nonce = 0
    prefix = f"{event_json}:{timestamp_ms}:"
    while True:
        digest = hashlib.sha256(f"{prefix}{nonce}".encode()).hexdigest()
        if digest.startswith(_POW_DIFFICULTY_PREFIX):
            return timestamp_ms, nonce
        nonce += 1


def _post_event(event: dict[str, Any]) -> bool:
    """Actually attempt the HTTP POST and log the outcome. Returns True on
    a 2xx response, False otherwise (network error, bad status, or no
    endpoint configured)."""
    import requests

    endpoint = load_config().get("telemetry_endpoint")
    if not isinstance(endpoint, str) or not secure_endpoint(endpoint):
        log_attempt("skipped_no_endpoint")
        _set_send_status(SendStatus("skipped_no_endpoint"))
        return False

    # Firebase treats JSON null as deletion semantics, while other collectors
    # may validate it as a present value. Optional telemetry fields therefore
    # use one portable representation: omit keys whose value is None.
    wire_event = {key: value for key, value in event.items() if value is not None}

    if "firebaseio.com" in endpoint:
        # The direct RTDB path is closed (auth != null alone can't stop
        # flooding - anonymous UIDs are free to mint). Any config still
        # pointed here is stale; config._merge_config migrates it forward on
        # the next load, but a send attempted before that happens must fail
        # cleanly rather than silently write nothing.
        log_attempt("skipped_legacy_endpoint")
        _set_send_status(SendStatus("skipped_legacy_endpoint"))
        return False

    if endpoint == config.TELEMETRY_GATEWAY_ENDPOINT:
        event_json = json.dumps(wire_event, sort_keys=True, separators=(",", ":"))
        timestamp_ms, nonce = _solve_proof_of_work(event_json)
        try:
            resp = requests.post(
                endpoint,
                json={"event_json": event_json, "timestamp": timestamp_ms, "nonce": nonce},
                timeout=10,
            )
        except requests.RequestException as e:
            detail = str(e)[:_MAX_FAILURE_DETAIL_LENGTH]
            log_attempt("send_failed_network", detail)
            _set_send_status(SendStatus("send_failed_network", detail=detail, retryable=True))
            return False
        if not (200 <= resp.status_code < 300):
            outcome = f"send_failed_http_{resp.status_code}"
            response_detail = str(getattr(resp, "text", "") or "")
            detail = " ".join(response_detail.split())[:_MAX_FAILURE_DETAIL_LENGTH]
            retryable = resp.status_code in {408, 425, 429} or resp.status_code >= 500
            log_attempt(outcome, detail)
            _set_send_status(SendStatus(outcome, status_code=resp.status_code, detail=detail, retryable=retryable))
            return False
        log_attempt("sent_ok")
        _set_send_status(SendStatus("sent_ok", status_code=resp.status_code))
        return True

    try:
        resp = requests.post(endpoint, json=wire_event, timeout=5)
    except requests.RequestException as e:
        detail = str(e)[:_MAX_FAILURE_DETAIL_LENGTH]
        log_attempt("send_failed_network", detail)
        _set_send_status(SendStatus("send_failed_network", detail=detail, retryable=True))
        return False
    if not (200 <= resp.status_code < 300):
        outcome = f"send_failed_http_{resp.status_code}"
        response_detail = str(getattr(resp, "text", "") or "")
        detail = " ".join(response_detail.split())[:_MAX_FAILURE_DETAIL_LENGTH]
        retryable = resp.status_code in {408, 425, 429} or resp.status_code >= 500
        log_attempt(outcome, detail)
        _set_send_status(
            SendStatus(
                outcome,
                status_code=resp.status_code,
                detail=detail,
                retryable=retryable,
            )
        )
        return False
    log_attempt("sent_ok")
    _set_send_status(SendStatus("sent_ok", status_code=resp.status_code))
    return True


def send_event(event: dict[str, Any], force: bool = False) -> bool:
    config_data = load_config()
    if not force and config_data.get("telemetry_send_policy") != "always":
        log_attempt("skipped_opt_out")
        _set_send_status(SendStatus("skipped_opt_out"))
        return False
    max_attempts = (
        _CONTRIBUTE_SEND_ATTEMPTS
        if event.get("benchmark_version") == 9
        and event.get("measurement_profile") == "contribute-v1"
        else 1
    )
    ok = False
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        ok = _post_event(event)
        status = last_send_status()
        if ok or status is None or not status.retryable:
            break
        if attempt < max_attempts:
            time.sleep(0.5 * (2 ** (attempt - 1)))
    status = last_send_status()
    if status is not None and status.attempts != attempts:
        _set_send_status(
            SendStatus(
                status.outcome,
                status_code=status.status_code,
                detail=status.detail,
                retryable=status.retryable,
                attempts=attempts,
            )
        )
    # Only persistent opt-in authorizes an unattended retry in a later
    # process. A one-shot --upload/confirmation authorizes this attempt, not
    # an indefinite background queue after the user may have opted out.
    if not ok and config_data.get("telemetry_send_policy") == "always":
        _append_pending(event)
    elif not ok and force:
        status = last_send_status() or SendStatus("send_failed_unknown", attempts=attempts)
        _save_last_failed(event, status)
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
