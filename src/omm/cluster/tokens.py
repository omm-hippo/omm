"""Per-employee access tokens for the cluster gateway.

Only sha256 digests are stored. A token store that cannot be read
authenticates nobody, and a corrupt file is never rewritten, so a hand-edit
gone wrong locks the gateway rather than opening it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

from omm import config
from omm.cluster.errors import ClusterError

TOKEN_PREFIX = "omm_"
_NAME_RE = re.compile(r"^[\w.-]{1,40}$")


class TokenError(ClusterError):
    pass


@dataclass(frozen=True)
class TokenRecord:
    name: str
    created_at: str


def tokens_path() -> Path:
    return config.OMM_HOME / "cluster" / "tokens.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse(text: str) -> list[dict]:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tokens"), list):
        raise ValueError("unexpected token file shape")
    rows = data["tokens"]
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get("name"), str)
                and isinstance(row.get("sha256"), str) and isinstance(row.get("created_at"), str)):
            raise ValueError("unexpected token row")
    return rows


def _read_rows() -> list[dict]:
    path = tokens_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        raise TokenError(f"cannot read {path}: {error}") from error
    try:
        return _parse(text)
    except ValueError as error:
        raise TokenError(f"{path} is damaged; fix or remove it by hand ({error})") from error


def _write_rows(rows: list[dict]) -> None:
    from omm.atomic import atomic_write_text

    path = tokens_path()
    atomic_write_text(path, json.dumps({"version": 1, "tokens": rows}, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)


def _lock():
    from omm.atomic import locked

    return locked(config.OMM_HOME / "locks" / "cluster-tokens")


def add_token(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise TokenError("token names are 1-40 letters, digits, '.', '-' or '_'")
    with _lock():
        rows = _read_rows()
        if any(row["name"] == name for row in rows):
            raise TokenError(f"a token named {name} already exists")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"name": name, "sha256": _digest(token), "created_at": created})
        _write_rows(rows)
    return token


def list_tokens() -> list[TokenRecord]:
    return [TokenRecord(row["name"], row["created_at"]) for row in _read_rows()]


def revoke_token(name: str) -> bool:
    with _lock():
        rows = _read_rows()
        kept = [row for row in rows if row["name"] != name]
        if len(kept) == len(rows):
            return False
        _write_rows(kept)
    return True


def _match(rows: list[dict], presented: str) -> str | None:
    if not isinstance(presented, str) or not presented.startswith(TOKEN_PREFIX):
        return None
    digest = _digest(presented)
    found = None
    for row in rows:
        if hmac.compare_digest(row["sha256"], digest):
            found = row["name"]
    return found


def verify(presented: str) -> str | None:
    try:
        rows = _read_rows()
    except TokenError:
        return None
    return _match(rows, presented)


class TokenStore:
    """Cached verifier for the gateway; reloads when the file changes."""

    def __init__(self) -> None:
        self._stamp: tuple[int, int] | None = None
        self._rows: list[dict] = []

    def verify(self, presented: str) -> str | None:
        path = tokens_path()
        try:
            info = path.stat()
        except OSError:
            self._stamp, self._rows = None, []
            return None
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self._stamp:
            try:
                rows = _read_rows()
            except TokenError:
                rows = []
            self._stamp, self._rows = stamp, rows
        return _match(self._rows, presented)
