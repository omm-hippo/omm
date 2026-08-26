"""Anonymous Firebase Auth for the hosted (`firebase_legacy`) telemetry
collector. Never raises - a token fetch failure just means the caller
(telemetry.py) skips that send, the same best-effort contract as the rest
of the telemetry path. The self-hosted FastAPI collector has no equivalent
and never calls into this module.

The RTDB rules require `auth != null` on telemetry writes so that a spam
write can at least be traced (and later blocked) by anonymous UID, rather
than accepted from anyone who finds the public REST endpoint. Since omm is
open source, no value embedded in the client can be a real secret - this
raises the bar for abuse, it does not eliminate it.
"""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.atomic import atomic_write_text, locked

_IDENTITY_TOOLKIT_SIGN_UP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
_SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
# Refresh a bit before actual expiry so a token doesn't die mid-request.
_EXPIRY_BUFFER_SECONDS = 300


def _cache_path() -> Path:
    return config.OMM_HOME / "firebase_auth.json"


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        with locked(path, timeout=10):
            loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, FileLockTimeout):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _harden_windows_file_permissions(path: Path) -> None:
    """Best-effort NTFS ACL restriction to the current user only.

    POSIX incidentally gets owner-only protection for this refresh-token
    cache from `tempfile.mkstemp`'s 0600 mode surviving `atomic_write_text`'s
    `os.replace` (rename preserves the temp file's mode). Windows has no
    permission-bit equivalent, so without this the file inherits whatever
    ACL its parent directory has - on a shared machine, potentially
    readable by other accounts. Best-effort and silent: `icacls` missing,
    insufficient privilege, or any other failure just means the token
    cache stays at the parent directory's default ACL - telemetry auth
    still works either way, matching this module's never-raises contract.
    """
    if platform.system() != "Windows":
        return
    username = os.environ.get("USERNAME")
    if not username:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _save_cache(session: dict[str, Any]) -> None:
    try:
        with locked(_cache_path(), timeout=10):
            atomic_write_text(_cache_path(), json.dumps(session))
        _harden_windows_file_permissions(_cache_path())
    except (OSError, FileLockTimeout):
        pass


def _sign_up_anonymously() -> dict[str, Any] | None:
    import requests

    try:
        resp = requests.post(
            _IDENTITY_TOOLKIT_SIGN_UP_URL,
            params={"key": config.FIREBASE_WEB_API_KEY},
            json={"returnSecureToken": True},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return _session_from(data.get("idToken"), data.get("refreshToken"), data.get("expiresIn"))


def _refresh(refresh_token: str) -> dict[str, Any] | None:
    import requests

    try:
        resp = requests.post(
            _SECURE_TOKEN_URL,
            params={"key": config.FIREBASE_WEB_API_KEY},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    # securetoken.googleapis.com replies snake_case, unlike identitytoolkit's
    # camelCase - both are Google's actual wire format, not a typo.
    return _session_from(data.get("id_token"), data.get("refresh_token"), data.get("expires_in"))


def _session_from(id_token: Any, refresh_token: Any, expires_in: Any) -> dict[str, Any] | None:
    if not isinstance(id_token, str) or not id_token:
        return None
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    try:
        expires_in = float(expires_in)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(expires_in) or expires_in <= 0:
        return None
    return {
        "id_token": id_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
    }


def get_id_token() -> str | None:
    """Best-effort anonymous ID token, reused from a cached, unexpired
    session where possible so most `omm` invocations don't cost a network
    round trip. Never raises."""
    cache = _load_cache()
    expires_at = cache.get("expires_at")
    id_token = cache.get("id_token")
    if (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
        and isinstance(id_token, str)
        and time.time() < expires_at - _EXPIRY_BUFFER_SECONDS
    ):
        return id_token

    refresh_token = cache.get("refresh_token")
    session = _refresh(refresh_token) if isinstance(refresh_token, str) else None
    if session is None:
        session = _sign_up_anonymously()
    if session is None:
        return None
    _save_cache(session)
    return session["id_token"]
