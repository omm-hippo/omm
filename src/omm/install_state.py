"""Durable install checkpoints. Files and links remain the source of truth."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import threading

from filelock import Timeout

from omm import config
from omm.atomic import atomic_write_text, backup_corrupt_file, locked
from omm.downloader import DownloadError

_CURRENT: ContextVar[InstallRecord | None] = ContextVar("omm_install_record", default=None)
_HELD_LOCKS: ContextVar[frozenset[tuple[Path, int]]] = ContextVar("omm_install_locks", default=frozenset())
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _path(filename: str) -> Path:
    # Case-insensitive locking also protects case-insensitive Mac/Windows hubs.
    key = hashlib.sha256(filename.casefold().encode("utf-8")).hexdigest()
    return config.OMM_HOME / "install-journal" / f"{key}.json"


def _source_key(resolved) -> str:
    identity = [resolved.url, resolved.provider, resolved.repo_id]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def read_record(filename: str) -> dict | None:
    path = _path(filename)
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 64 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("filename") != filename:
        return None
    return data


@dataclass
class InstallRecord:
    path: Path
    data: dict
    resumed: bool = False

    def save(self, **fields) -> None:
        self.data.update(fields)
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            atomic_write_text(self.path, json.dumps(self.data, indent=2) + "\n")
        except OSError as error:
            raise DownloadError(f"Could not persist the install checkpoint: {error}") from error


def current() -> InstallRecord | None:
    return _CURRENT.get()


def checkpoint(phase: str, **fields) -> None:
    record = current()
    if record is not None:
        record.save(phase=phase, **fields)


def verified_file_matches(filename: str, source_url: str, digest: str | None) -> bool:
    record = current()
    if record is None or record.data.get("filename") != filename:
        return False
    # Never accept a raw, unchecked download merely because a record exists.
    return (isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None
            and record.data.get("sha256") == digest
            and record.data.get("url_hash") == hashlib.sha256(source_url.encode()).hexdigest())


def preserve_interrupted_file(filename: str) -> bool:
    """Whether cancellation should retain a checkpointed artifact for retry.

    This only preserves bytes; reuse still requires a fresh checksum check.
    Runtime cleanup remains the responsibility of the install's finally blocks.
    """
    record = read_record(filename)
    return bool(record and record.get("status") == "interrupted"
                and isinstance(record.get("sha256"), str)
                and _DIGEST.fullmatch(record["sha256"]))


@contextmanager
def cleanup_guard(filename: str):
    """Hold the install lock throughout cleanup, in the same lock order."""
    path = _path(filename)
    if path.parent.is_symlink() or path.is_symlink():
        raise DownloadError("Refusing a symlinked install journal.")
    key = (path, threading.get_ident())
    if key in _HELD_LOCKS.get():
        yield
        return
    with locked(path, timeout=0):
        token = _HELD_LOCKS.set(_HELD_LOCKS.get() | {key})
        try:
            yield
        finally:
            _HELD_LOCKS.reset(token)


def pending_records() -> list[dict]:
    """Read-only diagnostics, including a process that died before recording failure."""
    root = config.OMM_HOME / "install-journal"
    if root.is_symlink() or not root.is_dir():
        return []
    pending = []
    for path in sorted(root.glob("*.json"))[:1024]:
        if path.is_symlink() or not _DIGEST.fullmatch(path.stem):
            continue
        try:
            if path.stat().st_size > 64 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(data, dict) and data.get("schema_version") == 1
                and isinstance(data.get("filename"), str)
                and _path(data["filename"]) == path
                and data.get("status") in {"running", "interrupted", "failed"}):
            pending.append(data)
    return pending


def tracked_install(func):
    @wraps(func)
    def wrapper(resolved, **kwargs):
        # The contribution loop retains its explicit per-candidate teardown.
        if kwargs.get("contribute_mode"):
            return func(resolved, **kwargs)
        from omm.hub import ModelResolutionError, validate_model_filename

        try:
            filename = validate_model_filename(resolved.filename)
        except ModelResolutionError as error:
            raise DownloadError(str(error)) from error
        path = _path(filename)
        if path.is_symlink() or path.parent.is_symlink():
            raise DownloadError("Refusing a symlinked install journal.")
        try:
            with cleanup_guard(filename):
                prior = read_record(filename)
                if prior is None and path.exists():
                    backup_corrupt_file(path)
                same_source = bool(prior and prior.get("source_key") == _source_key(resolved))
                resumed = bool(same_source and prior.get("status") in {"running", "interrupted", "failed"})
                record = InstallRecord(path, dict(prior) if resumed else {
                    "schema_version": 1, "filename": filename,
                    "source_key": _source_key(resolved),
                    "url_hash": hashlib.sha256(resolved.url.encode()).hexdigest(),
                    "phase": "preparing", "linked": {},
                }, resumed=resumed)
                record.save(status="running", last_error=None)
                token = _CURRENT.set(record)
                try:
                    result = func(resolved, **kwargs)
                    skipped = result.skipped_unfit or result.skipped_low_disk
                    missing_links = any(not result.linked.get(engine)
                                        for engine in record.data.get("target_engines", []))
                    record.save(status="skipped" if skipped else "failed" if missing_links else "complete",
                                runtime_status=result.compatibility_status)
                    return result
                except BaseException as error:
                    interrupted = isinstance(error, KeyboardInterrupt) or type(error).__name__ == "InstallInterrupted"
                    record.save(status="interrupted" if interrupted else "failed",
                                last_error=type(error).__name__)
                    raise
                finally:
                    _CURRENT.reset(token)
        except Timeout as error:
            raise DownloadError(f"Another install is already working on {filename}; retry after it finishes.") from error
    return wrapper
