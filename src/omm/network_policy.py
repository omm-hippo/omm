"""Process-wide outbound-network policy with loopback kept available.

The command layer selects a mode once per invocation.  The requests/socket
guards are a fail-closed backstop for missed call sites; user-facing command
paths still check the policy explicitly so they can explain the cache or next
step instead of surfacing a transport traceback.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

MODES = ("online", "models-only", "offline")

_mode = "online"
_purpose: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omm_network_purpose", default=None
)
_blocked: list[dict[str, str]] = []
_requests_installed = False
_socket_installed = False
_original_session_request = None
_original_getaddrinfo = None

_MODEL_HOSTS = frozenset(
    {
        "huggingface.co",
        "hf.co",
        "modelscope.cn",
        "raw.githubusercontent.com",
    }
)


class NetworkModeError(RuntimeError):
    pass


def validate_mode(value: object) -> str:
    return value if isinstance(value, str) and value in MODES else "online"


def set_mode(value: str) -> str:
    global _mode
    if value not in MODES:
        raise ValueError(f"network mode must be one of: {', '.join(MODES)}")
    _mode = value
    # Keep the normal online startup path as light as it was before this
    # feature: requests remains lazy until a restrictive mode actually needs
    # the process-wide backstop.
    if value != "online":
        install_guards()
    return value


def current_mode() -> str:
    return _mode


def read_saved_mode(config_path: Path) -> str:
    """Read without creating or repairing config.json."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "online"
    return validate_mode(data.get("network_mode") if isinstance(data, dict) else None)


def configure(config_path: Path, *, offline_override: bool = False) -> str:
    return set_mode("offline" if offline_override else read_saved_mode(config_path))


def _host_is_loopback(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _host_is_model_source(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").casefold()
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in _MODEL_HOSTS
    )


def _external_allowed(host: str | None) -> bool:
    if _mode == "online" or _host_is_loopback(host):
        return True
    if _mode == "models-only":
        return _purpose.get() == "model" or _host_is_model_source(host)
    return False


def request_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return _external_allowed(parsed.hostname)


def _blocked_message(action: str) -> str:
    if _mode == "offline":
        return (
            f"Network mode is offline, so OMM did not start {action}. "
            "Use installed models or a verified cache, or run "
            "`omm setting network --mode models-only` (model access only) "
            "or `--mode online`."
        )
    return (
        f"Network mode is models-only, so OMM did not start {action}. "
        "Model search/download remains available; use "
        "`omm setting network --mode online` for updates or data uploads."
    )


def require(purpose: str, action: str) -> None:
    allowed = (
        _mode == "online"
        or purpose == "local"
        or (_mode == "models-only" and purpose == "model")
    )
    if not allowed:
        _blocked.append({"purpose": purpose, "action": action})
        raise NetworkModeError(_blocked_message(action))


def uploads_allowed() -> bool:
    return _mode == "online"


def updates_allowed() -> bool:
    return _mode == "online"


def package_changes_allowed() -> bool:
    return _mode == "online"


def blocked_events() -> tuple[dict[str, str], ...]:
    return tuple(_blocked)


def clear_blocked_events() -> None:
    _blocked.clear()


@contextlib.contextmanager
def model_transfer():
    token = _purpose.set("model")
    try:
        yield
    finally:
        _purpose.reset(token)


def install_guards() -> None:
    global _requests_installed, _socket_installed
    global _original_session_request, _original_getaddrinfo

    if not _requests_installed:
        import requests

        _original_session_request = requests.sessions.Session.request

        def guarded_request(session, method, url, *args, **kwargs):
            if not request_allowed(str(url)):
                action = f"an external {str(method).upper()} request"
                _blocked.append({"purpose": _purpose.get() or "other", "action": action})
                raise requests.ConnectionError(_blocked_message(action))
            return _original_session_request(session, method, url, *args, **kwargs)

        requests.sessions.Session.request = guarded_request
        _requests_installed = True

    if not _socket_installed:
        _original_getaddrinfo = socket.getaddrinfo

        def guarded_getaddrinfo(host, *args, **kwargs):
            host_text = host.decode("ascii", "ignore") if isinstance(host, bytes) else str(host)
            if not _external_allowed(host_text):
                action = "an external DNS lookup"
                _blocked.append({"purpose": _purpose.get() or "other", "action": action})
                raise socket.gaierror(socket.EAI_FAIL, _blocked_message(action))
            return _original_getaddrinfo(host, *args, **kwargs)

        socket.getaddrinfo = guarded_getaddrinfo
        _socket_installed = True


def reset_for_tests() -> None:
    """Restore process globals. Tests only; production never changes mode twice."""
    global _mode, _requests_installed, _socket_installed
    global _original_session_request, _original_getaddrinfo
    if _requests_installed and _original_session_request is not None:
        import requests

        requests.sessions.Session.request = _original_session_request
    if _socket_installed and _original_getaddrinfo is not None:
        socket.getaddrinfo = _original_getaddrinfo
    _mode = "online"
    _requests_installed = False
    _socket_installed = False
    _original_session_request = None
    _original_getaddrinfo = None
    clear_blocked_events()
